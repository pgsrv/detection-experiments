import sys
import argparse

import cv2

from detector.ssd.utils.misc import Timer

from detector.ssd.mobilenetv3_ssd_lite import (
	create_mobilenetv3_large_ssd_lite,
	create_mobilenetv3_small_ssd_lite,
	create_mobilenetv3_ssd_lite_predictor
)

from storage.util import load


def draw_predictions(frame, boxes, labels, scores, class_names):
	for i in range(boxes.size(0)):
		box = boxes[i, :]
		cv2.rectangle(frame,
					  (box[0], box[1]), (box[2], box[3]),
					  (255, 255, 0), 4)

		label = f"{class_names[labels[i]]}: {scores[i]:.2f}"
		cv2.putText(frame, label,
					(box[0] + 20, box[1] + 40),
					cv2.FONT_HERSHEY_SIMPLEX,
					1,  # font scale
					(255, 0, 255),
					2)  # line type


def predict_and_show(orig_image, predictor, class_names, timer):
	image = cv2.cvtColor(orig_image, cv2.COLOR_BGR2RGB)

	timer.start("inference")
	boxes, labels, probs = predictor.predict(image, 10, 0.4)
	interval = timer.end("inference")
	print(f'Inference time: {interval:.3f}s, '
		f'Detect Objects: {labels.size(0)}.')

	draw_predictions(orig_image, boxes, labels, probs, class_names)

	cv2.imshow("result", orig_image)


def main():
	parser = argparse.ArgumentParser("Utility to process an image "
									 "through the detection model")

	parser.add_argument("--model-path", '-p', type=str, required=True,
						help="path to the trained model")
	parser.add_argument("--image", '-i', action='store_true',
						help="process on image")
	parser.add_argument("--video", '-v', action='store_true',
						help="process on video")
	parser.add_argument("path", type=str, nargs='?',
						help="file to process (use camera if omitted and "
						"'--video' is set")

	args = parser.parse_args()

	if args.image and args.video:
		print("Can process either image or video, but not both")
		sys.exit(-1)

	model, class_names = load(args.model_path)
	model.eval()

	predictor = create_mobilenetv3_ssd_lite_predictor(model, candidate_size=200)

	timer = Timer()

	if args.image:
		orig_image = cv2.imread(args.path)
		predict_and_show(orig_image, predictor, class_names, timer)
		cv2.waitKey(0)

	elif args.video:
		if len(args.path) > 0:
			cap = cv2.VideoCapture(args.path)  # capture from file
		else:
			cap = cv2.VideoCapture(0)   # capture from camera

		while True:
			ret, orig_image = cap.read()

			if not ret or orig_image is None:
				break

			predict_and_show(orig_image, predictor, class_names, timer)

			if cv2.waitKey(1) & 0xFF == ord('q'):
				break

		cap.release()

	cv2.destroyAllWindows()


if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		sys.exit(0)