# original code: https://github.com/dyhan0920/PyramidNet-PyTorch/blob/master/train.py

import os
import shutil
import time

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
from sklearn.model_selection._split import StratifiedShuffleSplit
from theconf.argument_parser import ConfigArgumentParser
from torch.utils.data.dataset import Subset
from tqdm._tqdm import tqdm

from network import resnet as RN
import network.pyramidnet as PYRM
from network.wideresnet import WideResNet as WRN
import utils
import warnings

from cutmix.cutmix import CutMix
from cutmix.utils import CutMixCrossEntropyLoss
from autoaug.archive import fa_reduced_cifar10, fa_reduced_imagenet, autoaug_paper_cifar10, autoaug_policy
from autoaug.augmentations import Augmentation

from rbg.model import get_model
import sys

warnings.filterwarnings("ignore")

model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))

"""
parser = ConfigArgumentParser(conflict_handler='resolve')
parser.add_argument('-j', '--workers', default=16, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--expname', default='TEST', type=str, help='name of experiment')
parser.add_argument('--cifarpath', default='/data/private/pretrainedmodels/', type=str)
parser.add_argument('--imagenetpath', default='/data/private/pretrainedmodels/imagenet/', type=str)
parser.add_argument('--autoaug', default='', type=str)
parser.add_argument('--cv', default=-1, type=int)
parser.add_argument('--only-eval', action='store_true')
parser.add_argument('--checkpoint', default='', type=str)

parser.set_defaults(bottleneck=True)
parser.set_defaults(verbose=True)
"""

best_err1 = 100
best_err5 = 100


class Args:
  def __init__(self):
    self.dataset = "cifar10"
    self.net_type = "resnet"
    self.depth = 20
    self.epochs = 200
    self.batch_size = 256
    self.lr = 0.1
    self.momentum = 0.9
    self.weight_decay = 0.0001
    self.cutmix_beta = 1.0
    self.cutmix_prob = 1.0
    self.cutmix_num = 1
    self.workers = 0
    self.expname = "TEST"
    self.cifarpath = "~/private/pretrainedmodels/"
    self.imagenetpath = "~/private/pretrainedmodels/"
    self.autoaug = ""
    self.cv = -1
    self.checkpoint = ""
    self.rbg = True


def main():
    global args, best_err1, best_err5
    # args = parser.parse_args()
    args = Args()
    if sys.argv[1] == "0":
        args.rbg = False
    args.dataset = sys.argv[2]
    args.expname = sys.argv[3]
    args.cut_mix_prob = float(sys.argv[4])
    print(args.rbg, args.dataset, args.expname, args.cut_mix_prob)

    if args.dataset.startswith('cifar'):
        normalize = transforms.Normalize(
            mean=[x / 255.0 for x in [125.3, 123.0, 113.9]],
            std=[x / 255.0 for x in [63.0, 62.1, 66.7]]
        )

        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ])

        autoaug = args.autoaug
        if autoaug:
            print('augmentation: %s' % autoaug)
            if autoaug == 'fa_reduced_cifar10':
                transform_train.transforms.insert(0, Augmentation(fa_reduced_cifar10()))
            elif autoaug == 'fa_reduced_imagenet':
                transform_train.transforms.insert(0, Augmentation(fa_reduced_imagenet()))
            elif autoaug == 'autoaug_cifar10':
                transform_train.transforms.insert(0, Augmentation(autoaug_paper_cifar10()))
            elif autoaug == 'autoaug_extend':
                transform_train.transforms.insert(0, Augmentation(autoaug_policy()))
            elif autoaug in ['default', 'inception', 'inception320']:
                pass
            else:
                raise ValueError('not found augmentations. %s' % C.get()['aug'])

        transform_test = transforms.Compose([
            transforms.ToTensor(),
            normalize
        ])

        if args.dataset == 'cifar100':
            ds_train = datasets.CIFAR100(args.cifarpath, train=True, download=True, transform=transform_train)
            if args.cv >= 0:
                sss = StratifiedShuffleSplit(n_splits=5, test_size=0.2, random_state=0)
                sss = sss.split(list(range(len(ds_train))), ds_train.targets)
                for _ in range(args.cv + 1):
                    train_idx, valid_idx = next(sss)
                ds_valid = Subset(ds_train, valid_idx)
                ds_train = Subset(ds_train, train_idx)
            else:
                ds_valid = Subset(ds_train, [])
            ds_test = datasets.CIFAR100(args.cifarpath, train=False, transform=transform_test)

            train_loader = torch.utils.data.DataLoader(
                CutMix(ds_train, 100, beta=args.cutmix_beta, prob=args.cutmix_prob, num_mix=args.cutmix_num),
                batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)
            tval_loader = torch.utils.data.DataLoader(ds_valid,
                 batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True)
            val_loader = torch.utils.data.DataLoader(ds_test,
                batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True)
            numberofclass = 100
        elif args.dataset == 'cifar10':
            ds_train = datasets.CIFAR10(args.cifarpath, train=True, download=True, transform=transform_train)
            if args.cv >= 0:
                sss = StratifiedShuffleSplit(n_splits=5, test_size=0.2, random_state=0)
                sss = sss.split(list(range(len(ds_train))), ds_train.targets)
                for _ in range(args.cv + 1):
                    train_idx, valid_idx = next(sss)
                ds_valid = Subset(ds_train, valid_idx)
                ds_train = Subset(ds_train, train_idx)
            else:
                ds_valid = Subset(ds_train, [])

            train_loader = torch.utils.data.DataLoader(
                CutMix(ds_train, 10,
                beta=args.cutmix_beta, prob=args.cutmix_prob, num_mix=args.cutmix_num),
                batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)
            tval_loader = torch.utils.data.DataLoader(ds_valid,
                batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True)
            val_loader = torch.utils.data.DataLoader(
                datasets.CIFAR10(args.cifarpath, train=False, transform=transform_test),
                batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)
            numberofclass = 10
        else:
            raise Exception('unknown dataset: {}'.format(args.dataset))

    elif args.dataset == 'imagenet':
        traindir = os.path.join(args.imagenetpath, 'train')
        valdir = os.path.join(args.imagenetpath, 'val')
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])

        jittering = utils.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4)
        lighting = utils.Lighting(alphastd=0.1,
                                  eigval=[0.2175, 0.0188, 0.0045],
                                  eigvec=[[-0.5675, 0.7192, 0.4009],
                                          [-0.5808, -0.0045, -0.8140],
                                          [-0.5836, -0.6948, 0.4203]])

        transform_train = transforms.Compose([
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            jittering,
            lighting,
            normalize,
        ])

        autoaug = args.autoaug
        if autoaug:
            print('augmentation: %s' % autoaug)
            if autoaug == 'fa_reduced_cifar10':
                transform_train.transforms.insert(0, Augmentation(fa_reduced_cifar10()))
            elif autoaug == 'fa_reduced_imagenet':
                transform_train.transforms.insert(0, Augmentation(fa_reduced_imagenet()))

            elif autoaug == 'autoaug_cifar10':
                transform_train.transforms.insert(0, Augmentation(autoaug_paper_cifar10()))
            elif autoaug == 'autoaug_extend':
                transform_train.transforms.insert(0, Augmentation(autoaug_policy()))
            elif autoaug in ['default', 'inception', 'inception320']:
                pass
            else:
                raise ValueError('not found augmentations. %s' % C.get()['aug'])

        train_dataset = datasets.ImageFolder(traindir, transform_train)
        if args.cv >= 0:
            sss = StratifiedShuffleSplit(n_splits=5, test_size=0.2, random_state=0)
            sss = sss.split(list(range(len(train_dataset))), train_dataset.targets)
            for _ in range(args.cv + 1):
                train_idx, valid_idx = next(sss)
            valid_dataset = Subset(train_dataset, valid_idx)
            train_dataset = Subset(train_dataset, train_idx)
        else:
            valid_dataset = Subset(train_dataset, [])

        train_dataset = CutMix(train_dataset, 1000, beta=args.cutmix_beta, prob=args.cutmix_prob, num_mix=args.cutmix_num)
        train_sampler = None

        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
            num_workers=args.workers, pin_memory=True, sampler=train_sampler)
        tval_loader = torch.utils.data.DataLoader(valid_dataset,
              batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True)
        val_loader = torch.utils.data.DataLoader(
            datasets.ImageFolder(valdir, transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                normalize,
            ])),
            batch_size=args.batch_size, shuffle=False,
            num_workers=args.workers, pin_memory=True)
        numberofclass = 1000
    else:
        raise Exception('unknown dataset: {}'.format(args.dataset))

    print("=> creating model '{}'".format(args.net_type))
    if args.net_type == 'resnet':
        if args.rbg:
            model, preprocess = get_model("resnet20", args.dataset, "rbg", 0.1)
        else:
            model, preprocess = get_model("resnet20", args.dataset, "scratch", 0.1)
        # model = RN.ResNet(args.dataset, args.depth, numberofclass, True)
    elif args.net_type == 'pyramidnet':
        model = PYRM.PyramidNet(args.dataset, args.depth, args.alpha, numberofclass, True)
    elif 'wresnet' in args.net_type:
        model = WRN(args.depth, args.alpha, dropout_rate=0.0, num_classes=numberofclass)
    else:
        raise ValueError('unknown network architecture: {}'.format(args.net_type))

    model = torch.nn.DataParallel(model).cuda()
    print('the number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()])))

    # define loss function (criterion) and optimizer
    criterion = CutMixCrossEntropyLoss(True)
    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=1e-4, nesterov=True)
    cudnn.benchmark = True

    for epoch in range(0, args.epochs):
        adjust_learning_rate(optimizer, epoch)

        # train for one epoch
        model.train()
        err1, err5, train_loss = run_epoch(train_loader, model, criterion, optimizer, epoch, 'train', True)
        # err1, err5, train_loss = run_epoch(train_loader, model, criterion, optimizer, epoch, 'train')
        train_err1 = err1
        err1, err5, train_loss = run_epoch(tval_loader, model, criterion, None, epoch, 'train-val', True)
        # err1, err5, train_loss = run_epoch(tval_loader, model, criterion, None, epoch, 'train-val')

        # evaluate on validation set
        model.eval()
        err1, err5, val_loss = run_epoch(val_loader, model, criterion, None, epoch, 'valid', True)
        # err1, err5, val_loss = run_epoch(val_loader, model, criterion, None, epoch, 'valid')

        # remember best prec@1 and save checkpoint
        is_best = err1 <= best_err1
        best_err1 = min(err1, best_err1)
        if is_best:
            best_err5 = err5
            print('Current Best (top-1 and 5 error):', best_err1, best_err5)

        save_checkpoint({
            'epoch': epoch,
            'arch': args.net_type,
            'state_dict': model.state_dict(),
            'best_err1': best_err1,
            'best_err5': best_err5,
            'optimizer': optimizer.state_dict(),
        }, is_best, filename='checkpoint_e%d_top1_%.3f_%.3f.pth' % (epoch, train_err1, err1))

    print('Best(top-1 and 5 error):', best_err1, best_err5)


def run_epoch(loader, model, criterion, optimizer, epoch, tag, rbg=False):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    end = time.time()
    if optimizer:
        current_lr = get_learning_rate(optimizer)[0]
    else:
        current_lr = None

    tqdm_disable = bool(os.environ.get('TASK_NAME', ''))  # for KakaoBrain
    loader = tqdm(loader, disable=tqdm_disable)
    loader.set_description('[%s %04d/%04d]' % (tag, epoch, args.epochs))

    for i, (input, target) in enumerate(loader):
        # measure data loading time
        data_time.update(time.time() - end)

        input, target = input.cuda(), target.cuda()

        if rbg:
            output, changed_target = model(input, target)
            loss = criterion(output, changed_target)
        else:
            output = model(input)
            loss = criterion(output, target)

        # measure accuracy and record loss
        losses.update(loss.item(), input.size(0))

        if len(target.size()) == 1:
            err1, err5 = accuracy(output.data, target, topk=(1, 5))
            top1.update(err1.item(), input.size(0))
            top5.update(err5.item(), input.size(0))

        if optimizer:
            # compute gradient and do SGD step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        else:
            del loss, output

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        loader.set_postfix(lr=current_lr, batch_time=batch_time.avg, data_time=data_time.avg, loss=losses.avg, top1=top1.avg, top5=top5.avg)

    if tqdm_disable:
        print('[%s %03d/%03d] %s' % (tag, epoch, args.epochs, dict(lr=current_lr, batch_time=batch_time.avg, data_time=data_time.avg, loss=losses.avg, top1=top1.avg, top5=top5.avg)))

    return top1.avg, top5.avg, losses.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    if not args.expname:
        return

    directory = "runs/%s/" % args.expname
    if not os.path.exists(directory):
        os.makedirs(directory)
    filename = directory + filename
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, os.path.join('runs', args.expname, 'model_best.pth'))


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    if args.dataset.startswith('cifar'):
        lr = args.lr * (0.1 ** (epoch // (args.epochs * 0.5))) * (0.1 ** (epoch // (args.epochs * 0.75)))
    elif args.dataset == 'imagenet':
        if args.epochs == 300:
            lr = args.lr * (0.1 ** (epoch // 75))
        else:
            lr = args.lr * (0.1 ** (epoch // 30))
    else:
        raise ValueError(args.dataset)

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def get_learning_rate(optimizer):
    lr = []
    for param_group in optimizer.param_groups:
        lr += [param_group['lr']]
    return lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
        wrong_k = batch_size - correct_k
        res.append(wrong_k.mul_(100.0 / batch_size))

    return res


if __name__ == '__main__':
    main()
