import torch
import torch.nn as nn
import math


# from .SE_weight_module import SEWeightModule

# Spatial Attention Module
class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()
        # Generate spatial attention weights
        self.conv1 = nn.Conv2d(2, 1, kernel_size=kernel_size, padding=(kernel_size - 1) // 2)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # Calculate the average and maximum values
        avg = torch.mean(x, dim=1, keepdim=True)
        max_, _ = torch.max(x, dim=1, keepdim=True)
        # Stack average and maximum values
        x = torch.cat([avg, max_], dim=1)
        # Generate spatial attention weights
        x = self.sigmoid(self.conv1(x))
        # Apply the weight to the input
        x_att = x
        return x_att


# Channel Attention Module
class ChannelAttention(nn.Module):
    def __init__(self, channels, reduction=16):
        super(ChannelAttention, self).__init__()
        mid_channel = channels // reduction
        # Adaptive pooling reduced feature map
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)

        self.shared_MLP = nn.Sequential(
               nn.Conv2d(channels, channels // reduction, kernel_size=1, padding=0),
               nn.ReLU(inplace=True),
               nn.Conv2d(channels // reduction, channels, kernel_size=1, padding=0)
        )
        self.sigmoid = nn.Sigmoid()
        # self.act=SiLU()

    def forward(self, x):
        avgout = self.shared_MLP(self.avg_pool(x))
        maxout = self.shared_MLP(self.max_pool(x))
        return self.sigmoid(avgout + maxout)


def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1, groups=1):
    """standard convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                     padding=padding, dilation=dilation, groups=groups, bias=False)


def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


class DMA(nn.Module):

    def __init__(self, inplans, planes, conv_kernels=[3, 5, 7, 9], stride=1, conv_groups=[1, 4, 8, 16]):
        super(DMA, self).__init__()
        self.conv_1 = conv(inplans, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[0] // 2,
                           stride=stride, groups=conv_groups[0])

        # c2  5*5
        self.conv_02 = conv(inplans, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[0] // 2,
                            stride=stride, groups=conv_groups[1], dilation=1)
        self.conv_2 = conv(planes // 4, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[0] // 2,
                           groups=conv_groups[1])

        # c3  7*7
        self.conv_03 = conv(inplans, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[1] // 2,
                            stride=stride, groups=conv_groups[2], dilation=2)
        self.conv_3 = conv(planes // 4, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[0] // 2,
                           groups=conv_groups[2])

        # c4  9*9
        self.conv_04 = conv(inplans, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[2] // 2,
                            stride=stride, groups=conv_groups[3], dilation=3)
        self.conv_4 = conv(planes // 4, planes // 4, kernel_size=conv_kernels[0], padding=conv_kernels[0] // 2,
                           groups=conv_groups[3])

        self.se = ChannelAttention(planes // 4)
        self.sa = SpatialAttention()

        self.split_channel = planes // 4
        self.softmax = nn.Softmax(dim=1)

        self.maxpool_2d = nn.MaxPool2d(kernel_size=3, stride=stride, padding=1)
        self.conv_5 = conv(inplans, planes // 4, kernel_size=1, padding=0)

    def forward(self, x):
        batch_size = x.shape[0]
        x1 = self.conv_1(x)

        # Dilated convolutional layers
        x_2 = self.conv_02(x)
        x_3 = self.conv_03(x)
        x_4 = self.conv_04(x)
        # Converge
        x2 = self.conv_2(x_2)
        x3 = self.conv_3(x_3)
        x4 = self.conv_4(x_4)
        x_5 = self.maxpool_2d(x)
        x5 = self.conv_5(x_5)

        # Hierarchical feature fusion (HFF)
        x3 = x3 + x2
        x4 = x3 + x4 + x5
        feats = torch.cat((x1, x2, x3, x4, x5), dim=1)
        feats = feats.view(batch_size, 5, self.split_channel, feats.shape[2], feats.shape[3])

        x1_se = self.se(x1)
        x2_se = self.se(x2)
        x3_se = self.se(x3)
        x4_se = self.se(x4)
        x5_se = self.se(x5)

        x_se = torch.cat((x1_se, x2_se, x3_se, x4_se, x5_se), dim=1)

        attention_vectors = x_se.view(batch_size, 5, self.split_channel, 1, 1)
        attention_vectors = self.softmax(attention_vectors)
        feats_weight = feats * attention_vectors
        for i in range(5):
            x_se_weight_fp = feats_weight[:, i, :, :]
            if i == 0:
                out_se = x_se_weight_fp
            else:
                out_se = torch.cat((x_se_weight_fp, out_se), 1)

        se_out = out_se



        # Gets the dimensions of the tensor
        batch_size, channels, height, width = se_out.size()
        # Divide the tensor into four different variables (spilt)
        part1 = se_out[:, :self.split_channel]
        part2 = se_out[:, self.split_channel:2 * self.split_channel]
        part3 = se_out[:, 2 * self.split_channel:3 * self.split_channel]
        part4 = se_out[:, 3 * self.split_channel:4 * self.split_channel]
        part5 = se_out[:, 4 * self.split_channel:]

        batch_size = x.shape[0]
        x1_sa = self.sa(part1)
        x2_sa = self.sa(part2)
        x3_sa = self.sa(part3)
        x4_sa = self.sa(part4)
        x5_sa = self.sa(part5)
        se_out = se_out.view(batch_size, 5, self.split_channel, se_out.shape[2], se_out.shape[3])
        x_sa = torch.cat((x1_sa, x2_sa, x3_sa, x4_sa, x5_sa), dim=1)
        attention_vectors = x_sa.view(batch_size, 5, 1, x_sa.shape[2], x_sa.shape[3])
        sa_feats_weight = se_out * attention_vectors
        for i in range(5):
            x_sa_weight_fp = sa_feats_weight[:, i, :, :]
            if i == 0:
                out_sa = x_sa_weight_fp
            else:
                out_sa = torch.cat((x_sa_weight_fp, out_sa), 1)

        sa_out = out_sa
        out = sa_out
        return out


class HPC_Block(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, norm_layer=None, conv_kernels=[3, 5, 7, 9],
                 conv_groups=[1, 4, 8, 16]):
        super(HPC_Block, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(inplanes, planes)
        self.bn1 = norm_layer(planes)
        self.conv2 = DMA(planes, planes, stride=stride, conv_kernels=conv_kernels, conv_groups=conv_groups)
        self.bn2 = norm_layer(planes // 4 * 5)
        self.conv3 = conv1x1(planes // 4 * 5, planes * self.expansion)
        self.bn3 = norm_layer(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)
        return out


class DMA_HPCNet(nn.Module):
    def __init__(self, block, layers, num_classes=2):
        super(DMA_HPCNet, self).__init__()
        self.inplanes = 64
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layers(block, 64, layers[0], stride=1)
        self.layer2 = self._make_layers(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layers(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layers(block, 512, layers[3], stride=2)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layers(self, block, planes, num_blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, num_blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)

        return x


def my_model(num_classes=2):
    model = DMA_HPCNet(HPC_Block, [3, 4, 6, 3], num_classes=2)
    return model


