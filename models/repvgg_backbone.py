import torch
import torch.nn as nn
import numpy as np

def conv_bn(in_channels, out_channels, kernel_size, stride, padding, groups=1):
    result = nn.Sequential()
    result.add_module('conv', nn.Conv2d(in_channels=in_channels, out_channels=out_channels,
                                        kernel_size=kernel_size, stride=stride, padding=padding, groups=groups, bias=False))
    result.add_module('bn', nn.BatchNorm2d(num_features=out_channels))
    return result

class RepVGGBlock(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size, stride=1, 
                 padding=0, dilation=1, groups=1, padding_mode='zeros', deploy=False):
        super(RepVGGBlock, self).__init__()
        self.deploy = deploy
        self.groups = groups
        self.in_channels = in_channels

        assert kernel_size == 3
        assert padding == 1

        padding_11 = padding - kernel_size // 2

        self.nonlinearity = nn.ReLU()

        if deploy:
            self.rbr_reparam = nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=stride,
                                         padding=padding, dilation=dilation, groups=groups, bias=True, padding_mode=padding_mode)

        else:
            self.rbr_identity = nn.BatchNorm2d(num_features=in_channels) if out_channels == in_channels and stride == 1 else None
            self.rbr_dense = conv_bn(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=stride, padding=padding, groups=groups)
            self.rbr_1x1 = conv_bn(in_channels=in_channels, out_channels=out_channels, kernel_size=1, stride=stride, padding=padding_11, groups=groups)


    def forward(self, inputs):
        if hasattr(self, 'rbr_reparam'):
            return self.nonlinearity(self.rbr_reparam(inputs))

        if self.rbr_identity is None:
            id_out = 0
        else:
            id_out = self.rbr_identity(inputs)

        return self.nonlinearity(self.rbr_dense(inputs) + self.rbr_1x1(inputs) + id_out)


    def _fuse_bn(self, branch):
        if branch is None:
            return 0, 0
        if isinstance(branch, nn.Sequential):
            kernel = branch.conv.weight.detach().cpu().numpy()
            running_mean = branch.bn.running_mean.cpu().numpy()
            running_var = branch.bn.running_var.cpu().numpy()
            gamma = branch.bn.weight.detach().cpu().numpy()
            beta = branch.bn.bias.detach().cpu().numpy()
            eps = branch.bn.eps
        else:
            assert isinstance(branch, nn.BatchNorm2d)
            kernel = np.zeros((self.in_channels, self.in_channels, 3, 3))
            for i in range(self.in_channels):
                kernel[i, i, 1, 1] = 1
            running_mean = branch.running_mean.cpu().numpy()
            running_var = branch.running_var.cpu().numpy()
            gamma = branch.weight.detach().cpu().numpy()
            beta = branch.bias.detach().cpu().numpy()
            eps = branch.eps
        std = np.sqrt(running_var + eps)
        t = gamma / std
        t = np.reshape(t, (-1, 1, 1, 1))
        t = np.tile(t, (1, kernel.shape[1], kernel.shape[2], kernel.shape[3]))
        return kernel * t, beta - running_mean * gamma / std

    def _pad_1x1_to_3x3(self, kernel1x1):
        if kernel1x1 is None:
            return 0
        kernel = np.zeros((kernel1x1.shape[0], kernel1x1.shape[1], 3, 3))
        kernel[:, :, 1:2, 1:2] = kernel1x1
        return kernel

    def repvgg_convert(self):
        kernel3x3, bias3x3 = self._fuse_bn(self.rbr_dense)
        kernel1x1, bias1x1 = self._fuse_bn(self.rbr_1x1)
        kernelid, biasid = self._fuse_bn(self.rbr_identity)
        return kernel3x3 + self._pad_1x1_to_3x3(kernel1x1) + kernelid, bias3x3 + bias1x1 + biasid

class REPVGGBackbone(nn.Module):

    def __init__(self, num_blocks=[2, 4, 14, 1], width_multiplier=[1.5, 1.5, 1.5, 2.75], override_groups_map=None, deploy=False, pretrained=True):
        super(REPVGGBackbone, self).__init__()

        assert len(width_multiplier) == 4

        self.deploy = deploy
        self.override_groups_map = override_groups_map or dict()

        assert 0 not in self.override_groups_map

        self.in_planes = min(64, int(64 * width_multiplier[0]))

        self.stage0 = RepVGGBlock(in_channels=3, out_channels=self.in_planes, kernel_size=3, stride=1, padding=1, deploy=self.deploy)
        self.cur_layer_idx = 1
        self.stage1 = self._make_stage(int(64 * width_multiplier[0]), num_blocks[0], stride=2)
        self.stage2 = self._make_stage(int(128 * width_multiplier[1]), num_blocks[1], stride=2)
        self.stage3 = self._make_stage(int(256 * width_multiplier[2]), num_blocks[2], stride=2)
        self.stage4 = self._make_stage(int(512 * width_multiplier[3]), num_blocks[3], stride=2)

        if pretrained:
            self.load_pre_trained_weights()

    def load_pre_trained_weights(self):
        print('Loading Pytorch pretrained weights...')
        pretrained_dict = state_dict = torch.load('/srv/tempdd/henzhang/weights/REGVGGPretrained/RepVGG-A2-train.pth')
        pretrained_dict.pop('linear.weight')
        pretrained_dict.pop('linear.bias')
        self.load_state_dict(pretrained_dict, strict=True)

    def _make_stage(self, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        blocks = []
        for stride in strides:
            cur_groups = self.override_groups_map.get(self.cur_layer_idx, 1)
            blocks.append(RepVGGBlock(in_channels=self.in_planes, out_channels=planes, kernel_size=3,
                                      stride=stride, padding=1, groups=cur_groups, deploy=self.deploy))
            self.in_planes = planes
            self.cur_layer_idx += 1
        return nn.Sequential(*blocks)

    def forward(self, x):
        x = self.stage0(x)
        x = self.stage1(x)
        x = self.stage2(x)
        out1 = self.stage3(x)
        out2 = self.stage4(out1)
        return out1, out2

#   Use like this:
#   train_model = create_RepVGG_A0(deploy=False)
#   train train_model
#   deploy_model = repvgg_convert(train_model, create_RepVGG_A0, save_path='repvgg_deploy.pth')
def repvgg_model_convert(model:torch.nn.Module, build_func, save_path=None):
    converted_weights = {}
    for name, module in model.named_modules():
        if hasattr(module, 'repvgg_convert'):
            kernel, bias = module.repvgg_convert()
            converted_weights[name + '.rbr_reparam.weight'] = kernel
            converted_weights[name + '.rbr_reparam.bias'] = bias
        elif isinstance(module, torch.nn.Linear):
            converted_weights[name + '.weight'] = module.weight.detach().cpu().numpy()
            converted_weights[name + '.bias'] = module.bias.detach().cpu().numpy()
        else:
            print(name, type(module))
    del model

    deploy_model = build_func(deploy=True)
    for name, param in deploy_model.named_parameters():
        print('deploy param: ', name, param.size(), np.mean(converted_weights[name]))
        param.data = torch.from_numpy(converted_weights[name]).float()

    if save_path is not None and save_path.endswith('pth'):
        torch.save(deploy_model.state_dict(), save_path)

    return deploy_model