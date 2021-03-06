#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import sys
import argparse
import math
import numpy as np
import cv2
import random
import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
from torch.autograd import Variable
import torch.utils.data as data
from data import AnnotationTransform, BaseTransform
from data import detection_collate, preproc
from utils import PriorBox, Detect
from utils import MultiBoxLoss
from utils import Timer
from utils.box import nms
cudnn.benchmark = True

### For Reproducibility ###
# import random
# SEED = 0
# random.seed(SEED)
# np.random.seed(SEED)
# torch.manual_seed(SEED)
# torch.cuda.manual_seed_all(SEED)
# torch.cuda.empty_cache()
# cudnn.benchmark = False
# cudnn.deterministic = True
# cudnn.enabled = True
### For Reproducibility ###

parser = argparse.ArgumentParser(description='Pytorch Training')
parser.add_argument('--neck', default='pafpn')
parser.add_argument('--backbone', default='repvgg')
parser.add_argument('--dataset', default='VOC')
parser.add_argument('--save_folder', default='weights/')
parser.add_argument('--mutual_guide', action='store_true')
parser.add_argument('--base_anchor_size', default=24.0, type=float)
parser.add_argument('--size', default=320, type=int)
parser.add_argument('--nms_thresh', default=0.5, type=float)
parser.add_argument('--batch_size', default=32, type=int)
parser.add_argument('--lr', default=1e-2, type=float)
parser.add_argument('--warm_iter', default=500, type=int)
parser.add_argument('--trained_model', help='Location to trained model')
parser.add_argument('--draw', action='store_true', help='Draw detection results')
args = parser.parse_args()
print(args)


def adjust_learning_rate(optimizer, epoch, iteration, warm_iter, max_iter):
    if iteration <= warm_iter:
        lr = 1e-6 + (args.lr - 1e-6) * iteration / warm_iter
    else:
        lr = 1e-6 + (args.lr - 1e-6) * 0.5 * (1 + math.cos((iteration - warm_iter) * math.pi / (max_iter - warm_iter)))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def tencent_trick(model):
    (decay, no_decay) = ([], [])
    for (name, param) in model.named_parameters():
        if not param.requires_grad:
            continue  # frozen weights
        elif len(param.shape) == 1 or name.endswith('.bias'):
            no_decay.append(param)
        else:
            decay.append(param)
    return [{'params': no_decay, 'weight_decay': 0.0}, {'params': decay}]


def load_dataset():
    if args.dataset == 'VOC':
        from data import VOCroot, VOCDetection, VOC_CLASSES
        show_classes = VOC_CLASSES
        num_classes = len(VOC_CLASSES)
        train_sets = [('2007', 'trainval'), ('2012', 'trainval')]
        dataset = VOCDetection(VOCroot, train_sets, preproc(args.size), AnnotationTransform(), dataset_name='VOC0712trainval')
        epoch_size = len(dataset) // args.batch_size
        max_iter = 250 * epoch_size
        testset = VOCDetection(VOCroot, [('2007', 'test')], None)
    elif args.dataset == 'COCO':
        from data import COCOroot, COCODetection, COCO_CLASSES
        show_classes = COCO_CLASSES
        num_classes = len(COCO_CLASSES)
        train_sets = [('2017', 'train')]
        dataset = COCODetection(COCOroot, train_sets, preproc(args.size))
        epoch_size = len(dataset) // args.batch_size
        max_iter = 140 * epoch_size
        testset = COCODetection(COCOroot, [('2017', 'val')], None)
    else:
        raise NotImplementedError('Unkown dataset {}!'.format(args.dataset))
    return (show_classes, num_classes, dataset, epoch_size, max_iter, testset)


def save_weights(model):
    save_path = os.path.join(args.save_folder, '{}_{}_{}_size{}_anchor{}{}.pth'.format(
        args.dataset,
        args.neck,
        args.backbone,
        args.size,
        args.base_anchor_size,
        ('_MG' if args.mutual_guide else ''),
        ))
    print('Saving to {}'.format(save_path))
    torch.save(model.state_dict(), save_path)


if __name__ == '__main__':

    print('Loading Dataset...')
    (show_classes, num_classes, dataset, epoch_size, max_iter, testset) =  load_dataset()

    print('Loading Network...')
    from models.detector import Detector
    model = Detector(args.size, num_classes, args.backbone, args.neck)
    model.train()
    model.cuda()
    num_param = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('Total param is : {:e}'.format(num_param))

    print('Preparing Optimizer & AnchorBoxes...')
    optimizer = optim.SGD(tencent_trick(model), lr=args.lr, momentum=0.9, weight_decay=0.0005)
    criterion = MultiBoxLoss(num_classes, mutual_guide=args.mutual_guide)
    priorbox = PriorBox(args.base_anchor_size, args.size)
    with torch.no_grad():
        priors = priorbox.forward()
        priors = priors.cuda()

    if args.trained_model is not None:
        print('loading weights from', args.trained_model)
        state_dict = torch.load(args.trained_model)
        model.load_state_dict(state_dict, strict=True)
    else:
        print('Training {}-{} on {} with {} images'.format(args.neck, args.backbone, dataset.name, len(dataset)))
        os.makedirs(args.save_folder, exist_ok=True)
        epoch = 0
        timer = Timer()
        for iteration in range(max_iter):
            if iteration % epoch_size == 0:

                # create batch iterator

                rand_loader = data.DataLoader(dataset, args.batch_size, shuffle=True, num_workers=4, collate_fn=detection_collate)
                batch_iterator = iter(rand_loader)
                epoch += 1

            timer.tic()
            adjust_learning_rate(optimizer, epoch, iteration, args.warm_iter, max_iter)
            (images, targets) = next(batch_iterator)
            images = Variable(images.cuda())
            targets = [Variable(anno.cuda()) for anno in targets]
            out = model(images)
            (loss_l, loss_c) = criterion(out, priors, targets)
            loss = loss_l + loss_c
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            load_time = timer.toc()

            if iteration % 100 == 0:
                print('Epoch {}, iter {}, lr {:.6f}, loss {:.2f}, time {:.2f}s, eta {:.2f}h'.format(
                    epoch,
                    iteration,
                    optimizer.param_groups[0]['lr'],
                    loss.item(),
                    load_time,
                    load_time * (max_iter - iteration) / 3600,
                    ))
                timer.clear()
        save_weights(model)
    
    print('Start Evaluation...')
    thresh=0.005
    max_per_image=300
    model.eval()
    detector = Detect(num_classes)
    transform = BaseTransform(args.size)
    num_images = len(testset)
    all_boxes = [[[] for _ in range(num_images)] for _ in range(num_classes)]
    rgbs = dict()
    os.makedirs("draw/", exist_ok=True)
    os.makedirs("draw/{}/".format(args.dataset), exist_ok=True)
    _t = {'im_detect': Timer(), 'im_nms': Timer()}
    for i in range(num_images):
        img = testset.pull_image(i)
        scale = torch.Tensor([img.shape[1], img.shape[0], img.shape[1], img.shape[0]])
        with torch.no_grad():
            x = transform(img).unsqueeze(0)
            (x, scale) = (x.cuda(), scale.cuda())

            _t['im_detect'].tic()
            out = model(x)  # forward pass
            (boxes, scores) = detector.forward(out, priors)
            detect_time = _t['im_detect'].toc()

        boxes *= scale  # scale each detection back up to the image
        boxes = boxes.cpu().numpy()
        scores = scores.cpu().numpy()

        _t['im_nms'].tic()
        for j in range(1, num_classes):
            inds = np.where(scores[:, j - 1] > thresh)[0]
            if len(inds) == 0:
                all_boxes[j][i] = np.empty([0, 5], dtype=np.float32)
                continue
            c_bboxes = boxes[inds]
            c_scores = scores[inds, j - 1]
            c_dets = np.hstack((c_bboxes, c_scores[:, np.newaxis])).astype(np.float32, copy=False)
            keep = nms(c_dets, thresh=args.nms_thresh)  # non maximum suppression
            c_dets = c_dets[keep, :]
            all_boxes[j][i] = c_dets
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1] for j in range(1, num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in range(1, num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]
        nms_time = _t['im_nms'].toc()

        if args.draw:
            for j in range(1, num_classes):
                c_dets = all_boxes[j][i]
                for line in c_dets:
                    x1 = int(line[0])
                    y1 = int(line[1])
                    x2 = int(line[2])
                    y2 = int(line[3])
                    score = float(line[4])
                    if score > .25:
                        if j not in rgbs:
                            r = random.randint(0,255)
                            g = random.randint(0,255)
                            b = random.randint(0,255)
                            rgbs[j] = [r,g,b]
                        rgb = rgbs[j]
                        label = '{}{:.2f}'.format(show_classes[j], score)
                        cv2.rectangle(img, (x1, y1), (x2, y2), rgb, 2)
                        cv2.rectangle(img, (x1, y1-15), (x1+len(label)*9, y1), rgb, -1)
                        img = cv2.putText(img, label, (x1, y1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1, cv2.LINE_AA)
            img = cv2.putText(img, 'Resolution {}x{} detect {:.2f}ms on {}'.format(args.size, args.size, detect_time*1000, torch.cuda.get_device_name(0)), (20, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,255), 1, cv2.LINE_AA)
            filename = 'draw/{}/{}.jpg'.format(args.dataset, i)
            cv2.imwrite(filename, img)

        if i == 10:
            _t['im_detect'].clear()
            _t['im_nms'].clear()
        if i % math.floor(num_images / 10) == 0 and i > 0:
            print('[{}/{}]Time results: detect={:.2f}ms,nms={:.2f}ms,'.format(i, num_images, detect_time * 1000, nms_time * 1000))
    testset.evaluate_detections(all_boxes)

