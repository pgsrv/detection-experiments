import os
import sys
import argparse
import logging
import itertools

import torch
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, MultiStepLR

from detector.ssd.utils.misc import Timer
from detector.ssd.mobilenetv3_ssd_lite import (
	create_mobilenetv3_large_ssd_lite,
	create_mobilenetv3_small_ssd_lite
)

from detector.ssd.multibox_loss import MultiboxLoss

from dataset.voc import VOCDetection
from dataset.coco import CocoDetection

from transform.collate import collate

import detector.ssd.config as config
from detector.ssd.data_preprocessing import TrainAugmentation, TestTransform

from storage.util import save

from optim.Ranger.ranger import Ranger
from optim.diffgrad.diffgrad import DiffGrad


torch.multiprocessing.set_sharing_strategy('file_system')


def train(loader, net, criterion, optimizer, device, epoch=-1):
	net.train(True)

	running_loss = 0.0
	running_reg_loss = 0.0
	running_cls_loss = 0.0
	num = 0

	for i, data in enumerate(loader):
		images = data["image"]
		boxes = data["bboxes"]
		labels = data["category_id"]

		images = images.to(device, dtype=torch.float32)
		boxes = [b.to(device, dtype=torch.float32) for b in boxes]
		labels = [l.to(device, dtype=torch.long) for l in labels]

		num += 1

		optimizer.zero_grad()
		confidence, locations = net(images)
		reg_loss, cls_loss = criterion(confidence, locations, labels, boxes)
		loss = reg_loss + cls_loss
		loss.backward()
		optimizer.step()

		running_loss += loss.item()
		running_reg_loss += reg_loss.item()
		running_cls_loss += cls_loss.item()

	avg_loss = running_loss / num
	avg_reg_loss = running_reg_loss / num
	avg_clf_loss = running_cls_loss / num

	logging.info(
		f"Epoch: {epoch}, Step: {i}, " +
		f"Average Loss: {avg_loss:.4f}, " +
		f"Average Regression Loss {avg_reg_loss:.4f}, " +
		f"Average Classification Loss: {avg_clf_loss:.4f}"
	)


def test(loader, net, criterion, device):
	net.eval()

	running_loss = 0.0
	running_reg_loss = 0.0
	running_cls_loss = 0.0
	num = 0

	for i, data in enumerate(loader):
		images = data["image"]
		boxes = data["bboxes"]
		labels = data["category_id"]

		images = images.to(device, dtype=torch.float32)
		boxes = [b.to(device, dtype=torch.float32) for b in boxes]
		labels = [l.to(device, dtype=torch.long) for l in labels]

		num += 1

		with torch.no_grad():
			confidence, locations = net(images)
			reg_loss, cls_loss = criterion(confidence, locations, labels, boxes)
			loss = reg_loss + cls_loss

		running_loss += loss.item()
		running_reg_loss += reg_loss.item()
		running_cls_loss += cls_loss.item()

	return running_loss / num, running_reg_loss / num, running_cls_loss / num


def main():
	parser = argparse.ArgumentParser(
		description='Single Shot MultiBox Detector Training With Pytorch')

	parser.add_argument('--dataset-style', type=str, required=True,
	                    help="style of dataset "
	                    "(supported are 'pascal-voc' and 'coco')")
	parser.add_argument('--dataset', required=True, help='dataset path')
	parser.add_argument('--train-image-set', type=str, default="train",
	                    help='image set (annotation file basename for COCO) '
	                    'to use for training')
	parser.add_argument('--val-image-set', type=str, default="val",
	                    help='image set (annotation file basename for COCO) '
	                    'to use for validation')
	parser.add_argument('--val-dataset', default=None,
	                    help='separate validation dataset directory path')

	parser.add_argument('--net', default="mb3-small-ssd-lite",
	                    help="network architecture "
	                    "(supported are mb3-large-ssd-lite and "
	                    "mb3-small-ssd-lite)")

	# Params for optimizer
	parser.add_argument('--optimizer', default="ranger",
	                    help="optimizer to use ('diffgrad', 'adamw', "
	                    "or 'ranger')")
	parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
	                    help='initial learning rate')

	parser.add_argument('--backbone-pretrained', action='store_true')
	parser.add_argument('--backbone-weights',
	                    help='pretrained weights for the backbone model')
	parser.add_argument('--freeze-backbone', action='store_true')

	# Scheduler
	parser.add_argument('--scheduler', default="cosine-wr", type=str,
	                    help="scheduler for SGD. It can one of 'multi-step'"
	                    "and 'cosine-wr'")

	# Params for Scheduler
	parser.add_argument('--milestones', default="80,100", type=str,
	                    help="milestones for MultiStepLR")
	parser.add_argument('--t0', default=10, type=int,
	                    help='T_0 value for Cosine Annealing Warm Restarts.')
	parser.add_argument('--t-mult', default=2, type=float,
	                    help='T_mult value for Cosine Annealing Warm Restarts.')

	# Train params
	parser.add_argument('--batch-size', default=32, type=int,
	                    help='batch size')
	parser.add_argument('--num-epochs', default=120, type=int,
	                    help='number of epochs to train')
	parser.add_argument('--num-workers', default=4, type=int,
	                    help='number of workers used in dataloading')
	parser.add_argument('--val-epochs', default=5, type=int,
	                    help='perform validation every this many epochs')
	parser.add_argument('--device', type=str,
	                    help='device to use for training')

	parser.add_argument('--checkpoint-path', default='output',
	                    help='directory for saving checkpoint models')


	logging.basicConfig(stream=sys.stdout, level=logging.INFO,
	                    format='%(asctime)s - %(levelname)s - %(message)s')

	args = parser.parse_args()
	logging.info(args)

	if args.device is None:
		device = "cuda" if torch.cuda.is_available() else "cpu"
	else:
		device = args.device

	if device.startswith("cuda"):
		logging.info("Use CUDA")

	timer = Timer()

	if args.net == 'mb3-large-ssd-lite':
		create_net = lambda num, pretrained: \
			create_mobilenetv3_large_ssd_lite(num, pretrained=pretrained)

	elif args.net == 'mb3-small-ssd-lite':
		create_net = lambda num, pretrained: \
			create_mobilenetv3_small_ssd_lite(num, pretrained=pretrained)

	else:
		logging.fatal("The net type is wrong.")
		parser.print_help(sys.stderr)
		sys.exit(1)

	if args.dataset_style == 'pascal-voc':
		bbox_format = 'pascal_voc'
	elif args.dataset_style == 'coco':
		bbox_format = 'coco'
	else:
		print("Dataset style %s is not supported" % args.dataset_style)
		sys.exit(-1)

	train_transform = TrainAugmentation(config.image_size,
	                                    config.image_mean, config.image_std,
	                                    bbox_format=bbox_format)

	test_transform = TestTransform(config.image_size,
	                               config.image_mean, config.image_std,
	                               bbox_format=bbox_format)

	logging.info("Loading datasets...")

	if args.dataset_style == 'pascal-voc':
		dataset = VOCDetection(root=args.dataset,
		                       image_set=args.train_image_set,
		                       transform=train_transform)
	elif args.dataset_style == 'coco':
		dataset = CocoDetection(root=args.dataset,
		                        ann_file="%s.json" % args.train_image_set,
		                        transform=train_transform)

	num_classes = len(dataset.class_names)

	logging.info("Train dataset size: {}".format(len(dataset)))

	# don't allow the last batch be of length 1
	# to not lead our dear BatchNorms to crash on that
	drop_last = len(dataset) % args.batch_size == 1

	train_loader = DataLoader(dataset, args.batch_size, collate_fn=collate,
	                          num_workers=args.num_workers,
	                          shuffle=True, drop_last=drop_last)

	if args.val_dataset is not None:
		val_dataset_root = args.val_dataset
	else:
		val_dataset_root = args.dataset

	if args.dataset_style == 'pascal-voc':
		val_dataset = VOCDetection(root=val_dataset_root,
		                           image_set=args.val_image_set,
		                           transform=test_transform)
	elif args.dataset_style == 'coco':
		val_dataset = CocoDetection(root=val_dataset_root,
		                            ann_file="%s.json" % args.val_image_set,
		                            transform=test_transform)

	logging.info("Validation dataset size: {}".format(len(val_dataset)))

	val_loader = DataLoader(val_dataset, args.batch_size, collate_fn=collate,
	                        num_workers=args.num_workers,
	                        shuffle=False)

	logging.info("Building network")
	net = create_net(num_classes,
	                 pretrained=(args.backbone_pretrained is not None))

	if args.backbone_pretrained and args.backbone_weights is not None:
		logging.info(f"Load backbone weights from {args.backbone_weights}")
		timer.start("Loading backbone model")
		net.load_backbone_weights(args.backbone_weights)
		logging.info(f'Took {timer.end("Loading backbone model"):.2f}s.')

	if args.freeze_backbone:
		net.freeze_backbone()

	net.to(device)

	last_epoch = -1

	priors = config.priors.to(device=device, dtype=torch.float32)
	criterion = MultiboxLoss(priors, iou_threshold=0.5, neg_pos_ratio=3,
	                         center_variance=0.1, size_variance=0.2)

	if args.optimizer == "adamw":
		optim_class = torch.optim.AdamW
	elif args.optimizer == "diffgrad":
		optim_class = DiffGrad
	else:
		optim_class = Ranger

	optimizer = optim_class(net.parameters(), lr=args.lr)
	logging.info(f"Learning rate: {args.lr}")

	if args.scheduler == 'multi-step':
		logging.info("Uses MultiStepLR scheduler.")
		milestones = [int(v.strip()) for v in args.milestones.split(",")]
		scheduler = MultiStepLR(optimizer, milestones=milestones,
		                        gamma=0.1, last_epoch=last_epoch)
	else:
		logging.info("Uses Cosine annealing warm restarts scheduler.")
		scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=args.t0,
		                                        T_mult=args.t_mult,
		                                        eta_min=1e-5)

	os.makedirs(args.checkpoint_path, exist_ok=True)

	logging.info(f"Start training from epoch {last_epoch + 1}.")
	for epoch in range(last_epoch + 1, args.num_epochs):
		train(train_loader, net, criterion,
		      optimizer, device=device, epoch=epoch)
		scheduler.step()

		if epoch % args.val_epochs == 0 or epoch == args.num_epochs - 1:
			val_loss, val_reg_loss, val_cls_loss = test(val_loader, net,
			                                            criterion, device)
			logging.info(
				f"Epoch: {epoch}, " +
				f"Validation Loss: {val_loss:.4f}, " +
				f"Validation Regression Loss {val_reg_loss:.4f}, " +
				f"Validation Classification Loss: {val_cls_loss:.4f}"
			)
			filename = f"{args.net}-Epoch-{epoch}-Loss-{val_loss}.pth"
			model_path = os.path.join(args.checkpoint_path, filename)
			save(net, dataset.class_names, model_path)
			logging.info(f"Saved model {model_path}")


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		sys.exit(0)
