import math
import pdb

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable


def import_class(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod

############################################################################################################
# Initialization

def conv_branch_init(conv, branches):
    weight = conv.weight
    n = weight.size(0)
    k1 = weight.size(1)
    k2 = weight.size(2)
    nn.init.normal_(weight, 0, math.sqrt(2. / (n * k1 * k2 * branches)))
    nn.init.constant_(conv.bias, 0)


def conv_init(conv):
    if conv.weight is not None:
        nn.init.kaiming_normal_(conv.weight, a=0.1, mode='fan_out', nonlinearity='leaky_relu')
    if conv.bias is not None:
        nn.init.constant_(conv.bias, 0)


def bn_init(bn, scale):
    nn.init.constant_(bn.weight, scale)
    nn.init.constant_(bn.bias, 0)


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        if hasattr(m, 'weight'):
            nn.init.kaiming_normal_(m.weight, a=0.1, mode='fan_out', nonlinearity='leaky_relu')
        if hasattr(m, 'bias') and m.bias is not None and isinstance(m.bias, torch.Tensor):
            nn.init.constant_(m.bias, 0)
    elif classname.find('BatchNorm') != -1:
        if hasattr(m, 'weight') and m.weight is not None:
            m.weight.data.normal_(1.0, 0.02)
        if hasattr(m, 'bias') and m.bias is not None:
            m.bias.data.fill_(0)

############################################################################################################
# Basic Layer

class TemporalConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1, groups=1):
        super(TemporalConv, self).__init__()
        pad = (kernel_size + (kernel_size-1) * (dilation-1) - 1) // 2
        self.conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=(kernel_size, 1),
            padding=(pad, 0),
            stride=(stride, 1),
            dilation=(dilation, 1),
            groups=groups)

        self.bn = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class unit_tcn(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=9, stride=1, groups=1):
        super(unit_tcn, self).__init__()
        pad = int((kernel_size - 1) / 2)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=(kernel_size, 1), padding=(pad, 0),
                              stride=(stride, 1), groups=groups)

        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.LeakyReLU(0.1)
        conv_init(self.conv)
        bn_init(self.bn, 1)

    def forward(self, x):
        x = self.bn(self.conv(x))
        return x
    
    
class UnfoldTemporalWindows(nn.Module):
    def __init__(self, window_size, window_stride, window_dilation=1, pad=True):
        super().__init__()
        self.window_size = window_size
        self.window_stride = window_stride
        self.window_dilation = window_dilation

        self.padding = (window_size + (window_size-1) * (window_dilation-1) - 1) // 2 if pad else 0
        self.unfold = nn.Unfold(kernel_size=(self.window_size, 1),
                                dilation=(self.window_dilation, 1),
                                stride=(self.window_stride, 1),
                                padding=(self.padding, 0))

    def forward(self, x):
        # Input shape: (N,C,T,V), out: (N,C,T,V*window_size)
        N, C, T, V = x.shape
        x = self.unfold(x)
        # Permute extra channels from window size to the graph dimension; -1 for number of windows
        x = x.view(N, C, self.window_size, -1, V).permute(0, 1, 3, 2, 4).contiguous()
        return x
    
############################################################################################################ 
# GC & TC Layer

class SGC(nn.Module):
    def __init__(self, in_channels, out_channels, rel_reduction=8, mid_reduction=1):
        super(SGC, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        if in_channels == 3 or in_channels == 9:
            self.rel_channels = 8
            self.mid_channels = 8
        else:
            self.rel_channels = in_channels // rel_reduction
            self.mid_channels = in_channels // mid_reduction
        self.num_group = 4 if self.in_channels != 3 else 1
        self.convQK = nn.Conv2d(self.in_channels, 2 * self.rel_channels, 1, groups=self.num_group)
        self.convV = nn.Conv2d(self.in_channels, self.out_channels, 1, groups=self.num_group)
        self.convc = nn.Conv2d(self.rel_channels, self.out_channels, 1, groups=self.num_group)
        self.tanh = nn.Tanh()
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)

    def forward(self, x, A=None, alpha=1):
        A = A.unsqueeze(0).unsqueeze(0) if A is not None else 0  # N,C,V,V
        q, k = self.convQK(x).mean(-2).chunk(2, 1)
        v = self.convV(x)
        weights = self.tanh(q.unsqueeze(-1) - k.unsqueeze(-2))
        weights = self.convc(weights) * alpha + A 
        x = torch.einsum('ncuv,nctv->nctu', weights, v)
        return x

    
class Temporal_Dynamic_Layer(nn.Module):
    def __init__(self, in_channels, out_channels, ws=3, stride=1, dilation=1, num_frame=64, residual=False):
        super(Temporal_Dynamic_Layer, self).__init__()

        rel_reduction = 8
        rel_channels = in_channels // rel_reduction if in_channels != 3 else 8
        
        self.num_group = 4 if in_channels != 3 else 1
        self.ws = ws
        self.unfold = UnfoldTemporalWindows(window_size=self.ws, 
                                            window_stride=stride, 
                                            window_dilation=dilation)
        self.conv1 = nn.Conv2d(in_channels, rel_channels, 1, groups=self.num_group) 
        self.conv2 = nn.Conv2d(self.ws, self.ws**2, 1)
        self.conv3 = nn.Sequential(
            nn.Conv2d(rel_channels, out_channels, 1, stride=1),
            nn.BatchNorm2d(out_channels))
        self.residual = nn.Sequential(
            nn.Conv2d(in_channels, rel_channels, 1, stride=(stride, 1), groups=self.num_group),
            nn.BatchNorm2d(rel_channels),
        ) if in_channels != out_channels or stride != 1 else (lambda x: x)
        self.bn = nn.BatchNorm2d(rel_channels)
        
        self.relu = nn.LeakyReLU(0.1)
        self.tanh = nn.Tanh()

    def forward(self, x):
        res = x
        v = self.conv1(x)
        x, v = self.unfold(x), self.unfold(v) #nctwv
        N, C, T, W, V = x.size()
        x = x.mean(1).transpose(1, 2).contiguous()
        weights = self.tanh(self.conv2(x).view(N, W, W, T, V))
        x = torch.einsum('nwutv,nctuv->nctv', weights, v)
        x = self.relu(self.bn(x) + self.residual(res))
        x = self.conv3(x)
        return x   
    
############################################################################################################ 
# GC & TC Module

class unit_gcn(nn.Module):
    def __init__(self, in_channels, out_channels, A, coff_embedding=4, adaptive=True, residual=True):
        super(unit_gcn, self).__init__()
        inter_channels = out_channels // coff_embedding
        self.inter_c = inter_channels
        self.out_c = out_channels
        self.in_c = in_channels
        self.adaptive = adaptive
        self.num_subset = A.shape[0]
        self.convs = nn.ModuleList()
        for i in range(self.num_subset):
            self.convs.append(SGC(in_channels, out_channels))
        self.num_group = 4 if in_channels != 3 else 1
        if residual:
            if in_channels != out_channels:
                self.down = nn.Sequential(
                    nn.Conv2d(in_channels, out_channels, 1, groups=self.num_group),
                    nn.BatchNorm2d(out_channels)
                )
            else:
                self.down = lambda x: x
        else:
            self.down = lambda x: 0
        if self.adaptive:
            self.PA = nn.Parameter(torch.from_numpy(A.astype(np.float32)))
        else:
            self.A = Variable(torch.from_numpy(A.astype(np.float32)), requires_grad=False)
        self.alpha = nn.Parameter(torch.zeros(1))
        self.bn = nn.BatchNorm2d(out_channels)
        self.soft = nn.Softmax(-2)
        self.relu = nn.LeakyReLU(0.1)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)
        bn_init(self.bn, 1e-6)

    def forward(self, x):
        y = None
        if self.adaptive:
            A = self.PA
        else:
            A = self.A.cuda(x.get_device())
        for i in range(self.num_subset):
            z = self.convs[i](x, A[i], self.alpha)
            y = z + y if y is not None else z
        y = self.bn(y)
        y += self.down(x)
        y = self.relu(y)
        return y
    
    
class MultiScale_TemporalConv(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 stride=1,
                 dilations=[1,2,3,4],
                 residual=True,
                 residual_kernel_size=1, 
                 num_frame=64):

        super().__init__()
        assert out_channels % (len(dilations) + 2) == 0, '# out channels should be multiples of # branches'

        # Multiple branches of temporal convolution
        self.num_branches = len(dilations) + 2
        branch_channels = out_channels // self.num_branches
        if type(kernel_size) == list:
            assert len(kernel_size) == len(dilations)
        else:
            kernel_size = [kernel_size]*len(dilations)
        # Temporal Convolution branches
        self.num_group = 4 if in_channels != 3 else 1
        self.branches = nn.ModuleList([
            Temporal_Dynamic_Layer(in_channels, 
                                   branch_channels, 
                                   ws=ks, 
                                   stride=stride, 
                                   dilation=dilation, 
                                   num_frame=num_frame, 
                                   residual=False)
            for ks, dilation in zip(kernel_size, dilations)
        ])

        # Additional Max & 1x1 branch
        self.branches.append(nn.Sequential(
            nn.Conv2d(in_channels, branch_channels, kernel_size=1, padding=0),
            nn.BatchNorm2d(branch_channels),
            nn.LeakyReLU(0.1),
            nn.MaxPool2d(kernel_size=(3,1), stride=(stride,1), padding=(1,0)),
            nn.BatchNorm2d(branch_channels) 
        ))

        self.branches.append(nn.Sequential(
            nn.Conv2d(in_channels, branch_channels, kernel_size=1, padding=0, stride=(stride,1)),
            nn.BatchNorm2d(branch_channels)
        ))

        # Residual connection
        if not residual:
            self.residual = lambda x: 0
        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x
        else:
            self.residual = TemporalConv(in_channels, out_channels, 
                                         kernel_size=residual_kernel_size, stride=stride)

        # initialize
        self.apply(weights_init)

    def forward(self, x):
        # Input dim: (N,C,T,V)
        res = self.residual(x)
        branch_outs = []
        for tempconv in self.branches:
            out = tempconv(x)
            branch_outs.append(out)

        out = torch.cat(branch_outs, dim=1)
        out += res
        return out

############################################################################################################ 
# Block & Network
    
class TCN_GCN_unit(nn.Module):
    def __init__(self, in_channels, out_channels, A, stride=1, 
                 residual=True, adaptive=True, kernel_size=5, dilations=[1,2], num_frame=64):
        super(TCN_GCN_unit, self).__init__()
        self.gcn1 = unit_gcn(in_channels, out_channels, A, adaptive=adaptive)
        self.tcn1 = MultiScale_TemporalConv(out_channels, 
                                            out_channels, 
                                            kernel_size=kernel_size, 
                                            stride=stride, 
                                            dilations=dilations, 
                                            num_frame=num_frame, 
                                            residual=False)

        self.relu = nn.LeakyReLU(0.1)
        self.num_group = 4 if in_channels != 3 else 1
        
        if not residual:
            self.residual = lambda x: 0
        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x
        else:
            self.residual = unit_tcn(in_channels, out_channels, kernel_size=1, stride=stride, groups=self.num_group)

    def forward(self, x):
        res = x
        x = self.gcn1(x)
        x = self.tcn1(x)
        x = self.relu(x + self.residual(res))
        return x
    
    
class TSAGCN(nn.Sequential):
    def __init__(self, block_args, A):
        super(SDTGCN, self).__init__()
        for i, [in_channels, out_channels, stride, residual, adaptive, num_frame] in enumerate(block_args):
            self.add_module(f'block-{i}_tcngcn', TCN_GCN_unit(in_channels, 
                                                              out_channels, 
                                                              A, 
                                                              stride=stride, 
                                                              residual=residual, 
                                                              adaptive=adaptive, 
                                                              num_frame=num_frame))  


class Model(nn.Module):
    def __init__(self, num_class=60, num_point=25, num_person=2, graph=None, graph_args=dict(), in_channels=3,
                 drop_out=0, adaptive=True):
        super(Model, self).__init__()

        if graph is None:
            raise ValueError()
        else:
            Graph = import_class(graph)
            self.graph = Graph(**graph_args)

        A = self.graph.A # 3,25,25

        self.num_class = num_class
        self.num_point = num_point
        self.data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)

        base_channel = 64
        self.blockargs = [
            [in_channels, base_channel, 1, False, adaptive, 64],
            [base_channel, base_channel, 1, True, adaptive, 64],
            [base_channel, base_channel, 1, True, adaptive, 64],
            [base_channel, base_channel, 1, True, adaptive, 64],
            [base_channel, base_channel*2, 2, True, adaptive, 64],
            [base_channel*2, base_channel*2, 1, True, adaptive, 32],
            [base_channel*2, base_channel*2, 1, True, adaptive, 32],
            [base_channel*2, base_channel*4, 2, True, adaptive, 32],
            [base_channel*4, base_channel*4, 1, True, adaptive, 16],
            [base_channel*4, base_channel*4, 1, True, adaptive, 16]
        ]
        
        self.num_layer = 3
        self.layer = nn.ModuleList([TSAGCN(self.blockargs, A) for _ in range(self.num_layer)])
        self.fc = nn.ModuleList([nn.Linear(base_channel*4, num_class) for _ in range(self.num_layer)])
        
        for fc in self.fc:
            nn.init.normal_(fc.weight, 0, math.sqrt(2. / num_class))
        bn_init(self.data_bn, 1)        
        
        if drop_out:
            self.drop_out = nn.Dropout(drop_out)
        else:
            self.drop_out = lambda x: x

    def forward(self, x):
        if len(x.shape) == 3:
            N, T, VC = x.shape
            x = x.view(N, T, self.num_point, -1).permute(0, 3, 1, 2).contiguous().unsqueeze(-1)
        N, C, T, V, M = x.size()

        x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V)
        
        x_ = x  
        out = []
        for layer, fc in zip(self.layer, self.fc):
            x = x_
            x = layer(x)
            c_new = x.size(1)
            x = x.view(N, M, c_new, -1)
            x = x.mean(3).mean(1)
            x = self.drop_out(x)
            out.append(fc(x))

        return out
