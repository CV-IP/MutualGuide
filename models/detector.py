#!/usr/bin/python
# -*- coding: utf-8 -*-
import os
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F

def multibox(fpn_level, num_anchors, num_classes, fea_channel):
    (loc_layers, conf_layers) = ([], [])
    loc_channel = num_anchors * 4
    cls_channel = num_anchors * num_classes
    for _ in range(fpn_level):
        loc_layers += [nn.Conv2d(fea_channel, loc_channel, kernel_size=3, padding=1)]
        conf_layers += [nn.Conv2d(fea_channel, cls_channel, kernel_size=3, padding=1)]
    return (nn.ModuleList(loc_layers), nn.ModuleList(conf_layers))


class Detector(nn.Module):

    def __init__(self, size, num_classes, backbone, neck):
        super(Detector, self).__init__()

        # Params
        if not size % 64 == 0:
            raise ValueError('Error: Sorry size {} is not supported!'.format(size))
        self.fpn_level = (4 if size < 512 else 5)
        self.num_classes = num_classes - 1
        self.num_anchors = 6

        # Backbone network

        if backbone == 'vgg16':
            from models.backbone.vgg_backbone import VGGBackbone
            self.backbone = VGGBackbone(pretrained=True)
            channels = (512, 512)
            fea_channel = 256
        elif backbone == 'repvgg':
            from models.backbone.repvgg_backbone import REPVGGBackbone
            self.backbone = REPVGGBackbone(pretrained=True)
            channels = (384, 1408)
            fea_channel = 256
        elif backbone == 'resnet18':
            from models.backbone.resnet_backbone import ResNetBackbone
            self.backbone = ResNetBackbone(pretrained=True)
            channels = (256, 512)
            fea_channel = 256
        elif backbone == 'shufflenet':
            from models.backbone.shufflenet_backbone import ShuffleNetBackbone
            self.backbone = ShuffleNetBackbone(pretrained=True)
            channels = (232, 464)
            fea_channel = 128
        else:
            raise ValueError('Error: Sorry backbone {} is not supported!'.format(backbone))

        # Neck network
        if neck == 'ssd':
            from models.neck.ssd_neck import SSDNeck
            self.neck = SSDNeck(self.fpn_level, channels, fea_channel)
        elif neck == 'fpn':
            from models.neck.fpn_neck import FPNNeck
            self.neck = FPNNeck(self.fpn_level, channels, fea_channel)
        elif neck == 'pafpn':
            from models.neck.pafpn_neck import PAFPNNeck
            self.neck = PAFPNNeck(self.fpn_level, channels, fea_channel)
        else:
            raise ValueError('Error: Sorry neck {} is not supported!'.format(neck))

        # Detection Head

        (self.loc, self.conf) = multibox(self.fpn_level, self.num_anchors, self.num_classes, fea_channel)

        bias_value = 0
        for modules in self.loc:
            for l in modules.modules():
                if isinstance(l, nn.Conv2d):
                    torch.nn.init.normal_(l.weight, std=0.01)
                    torch.nn.init.constant_(l.bias, bias_value)
        import math
        prior_prob = 0.01
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        for modules in self.conf:
            for l in modules.modules():
                if isinstance(l, nn.Conv2d):
                    torch.nn.init.normal_(l.weight, std=0.01)
                    torch.nn.init.constant_(l.bias, bias_value)

    def forward(self, x):
        loc = list()
        conf = list()

        x = self.backbone(x)
        fea = self.neck(x)

        for (i, (x, l, c)) in enumerate(zip(fea, self.loc, self.conf)):
            loc.append(l(x).permute(0, 2, 3, 1).contiguous())
            conf.append(c(x).permute(0, 2, 3, 1).contiguous())

        loc = torch.cat([o.view(o.size(0), -1) for o in loc], 1)
        conf = torch.cat([o.view(o.size(0), -1) for o in conf], 1)
        return (loc.view(loc.size(0), -1, 4), conf.view(conf.size(0), -1, self.num_classes))

