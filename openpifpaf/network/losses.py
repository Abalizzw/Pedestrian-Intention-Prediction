"""Losses."""

from abc import ABCMeta, abstractstaticmethod
import logging
import re
import torch

from ..data import (COCO_PERSON_SIGMAS, COCO_PERSON_SKELETON, KINEMATIC_TREE_SKELETON,
                    DENSER_COCO_PERSON_SKELETON)

LOG = logging.getLogger(__name__)


class Loss(metaclass=ABCMeta):
    @abstractstaticmethod
    def match(head_name):  # pylint: disable=unused-argument
        return False

    @classmethod
    def cli(cls, parser):
        """Add decoder specific command line arguments to the parser."""

    @classmethod
    def apply_args(cls, args):
        """Read command line arguments args to set class properties."""


def laplace_loss(x1, x2, logb, t1, t2, weight=None):
    """Loss based on Laplace Distribution.

    Loss for a single two-dimensional vector (x1, x2) with radial
    spread b and true (t1, t2) vector.
    """
    norm = torch.sqrt((x1 - t1)**2 + (x2 - t2)**2)
    losses = 0.694 + logb + norm * torch.exp(-logb)
    if weight is not None:
        losses = losses * weight
    return torch.sum(losses)


def l1_loss(x1, x2, _, t1, t2, weight=None):
    """L1 loss.

    Loss for a single two-dimensional vector (x1, x2)
    true (t1, t2) vector.
    """
    losses = torch.sqrt((x1 - t1)**2 + (x2 - t2)**2)
    if weight is not None:
        losses = losses * weight
    return torch.sum(losses)


class SmoothL1Loss(object):
    def __init__(self, r_smooth, scale_required=True):
        self.r_smooth = r_smooth
        self.scale = None
        self.scale_required = scale_required

    def __call__(self, x1, x2, _, t1, t2, weight=None):
        """L1 loss.

        Loss for a single two-dimensional vector (x1, x2)
        true (t1, t2) vector.
        """
        if self.scale_required and self.scale is None:
            raise Exception
        if self.scale is None:
            self.scale = 1.0

        r = self.r_smooth * self.scale
        d = torch.sqrt((x1 - t1)**2 + (x2 - t2)**2)
        smooth_regime = d < r

        smooth_loss = 0.5 / r[smooth_regime] * d[smooth_regime] ** 2
        linear_loss = d[smooth_regime == 0] - (0.5 * r[smooth_regime == 0])
        losses = torch.cat((smooth_loss, linear_loss))

        if weight is not None:
            losses = losses * weight

        self.scale = None
        return torch.sum(losses)


class MultiHeadLoss(torch.nn.Module):
    def __init__(self, losses, lambdas):
        super(MultiHeadLoss, self).__init__()

        self.losses = torch.nn.ModuleList(losses)
        self.losses_pifpaf = self.losses[:2]
        self.losses_crm    = self.losses[2:]
        self.lambdas = lambdas

    def forward(self, head_fields, head_targets, head):  # pylint: disable=arguments-differ
    
        if head=="pifpaf":
    
            assert len(self.losses_pifpaf) == len(head_fields)
            assert len(self.losses_pifpaf) <= len(head_targets)
            flat_head_losses = [ll
                                for l, f, t in zip(self.losses_pifpaf, head_fields, head_targets)
                                for ll in l(f, t)]
            assert len(self.lambdas) == len(flat_head_losses)
            loss_values = [lam * l
                           for lam, l in zip(self.lambdas, flat_head_losses)
                           if l is not None]
            total_loss = sum(loss_values) if loss_values else None
        
        if head=="crm":
            
            assert len(self.losses_crm) == len(head_fields)
            assert len(self.losses_crm) <= len(head_targets)
            flat_head_losses = [ll
                                for l, f, t in zip(self.losses_crm, head_fields, head_targets)
                                for ll in l(f, t)]
            loss_values = [0.2 * l
                           for l in flat_head_losses
                           if l is not None]
            total_loss = sum(loss_values) if loss_values else None
        return total_loss, flat_head_losses

class CompositeLoss(Loss, torch.nn.Module):
    default_background_weight = 1.0
    default_multiplicity_correction = False
    default_independence_scale = 3.0

    def __init__(self, head_name, regression_loss, *,
                 n_vectors=None, n_scales=None, sigmas=None):
        super(CompositeLoss, self).__init__()

        if n_vectors is None and 'pif' in head_name:
            n_vectors = 1
        if n_vectors is None and 'paf' in head_name:
            n_vectors = 2

        if n_scales is None and 'pif' in head_name:
            n_scales = 1
        if n_scales is None and 'paf' in head_name:
            n_scales = 0

        if sigmas is None and head_name == 'pif':
            sigmas = [COCO_PERSON_SIGMAS]
        if sigmas is None and 'pif' in head_name:
            sigmas = [[1.0]]
        if sigmas is None and head_name in ('paf', 'paf19', 'wpaf'):
            sigmas = [
                [COCO_PERSON_SIGMAS[j1i - 1] for j1i, _ in COCO_PERSON_SKELETON],
                [COCO_PERSON_SIGMAS[j2i - 1] for _, j2i in COCO_PERSON_SKELETON],
            ]
        if sigmas is None and head_name in ('paf16',):
            sigmas = [
                [COCO_PERSON_SIGMAS[j1i - 1] for j1i, _ in KINEMATIC_TREE_SKELETON],
                [COCO_PERSON_SIGMAS[j2i - 1] for _, j2i in KINEMATIC_TREE_SKELETON],
            ]
        if sigmas is None and head_name in ('paf44',):
            sigmas = [
                [COCO_PERSON_SIGMAS[j1i - 1] for j1i, _ in DENSER_COCO_PERSON_SKELETON],
                [COCO_PERSON_SIGMAS[j2i - 1] for _, j2i in DENSER_COCO_PERSON_SKELETON],
            ]

        self.background_weight = self.default_background_weight
        self.multiplicity_correction = self.default_multiplicity_correction
        self.independence_scale = self.default_independence_scale

        self.n_vectors = n_vectors
        self.n_scales = n_scales
        if self.n_scales:
            assert len(sigmas) == n_scales
            
        #print("losses.py ", head_name, sigmas)
            
        LOG.debug('%s: n_vectors = %d, n_scales = %d, len(sigmas) = %d',
                  head_name, n_vectors, n_scales, len(sigmas))

        if sigmas is not None:
            assert len(sigmas) == n_vectors
            scales_to_kp = torch.tensor(sigmas)
            scales_to_kp = torch.unsqueeze(scales_to_kp, 0)
            scales_to_kp = torch.unsqueeze(scales_to_kp, -1)
            scales_to_kp = torch.unsqueeze(scales_to_kp, -1)
            self.register_buffer('scales_to_kp', scales_to_kp)
        else:
            self.scales_to_kp = None

        self.regression_loss = regression_loss or laplace_loss

    @staticmethod
    def match(head_name):
        return head_name in (
            'pif',
            'paf',
            'pafs',
            'wpaf',
            'pafb',
            'pafs19',
            'pafsb',
        ) or re.match('p[ia]f([0-9]+)$', head_name) is not None

    @classmethod
    def cli(cls, parser):
        # group = parser.add_argument_group('composite loss')
        pass

    @classmethod
    def apply_args(cls, args):
        cls.default_background_weight = args.background_weight
        cls.default_fixed_size = args.paf_fixed_size
        cls.default_aspect_ratio = args.paf_aspect_ratio

    def forward(self, x, t):  # pylint: disable=arguments-differ
        
        assert len(x) == 1 + 2 * self.n_vectors + self.n_scales
        x_intensity = x[0]
        x_regs = x[1:1 + self.n_vectors]
        x_spreads = x[1 + self.n_vectors:1 + 2 * self.n_vectors]
        x_scales = []
        if self.n_scales:
            x_scales = x[1 + 2 * self.n_vectors:1 + 2 * self.n_vectors + self.n_scales]

        assert len(t) == 1 + self.n_vectors + 1
        target_intensity = t[0]
        target_regs = t[1:1 + self.n_vectors]
        target_scale = t[-1]

        bce_masks = torch.sum(target_intensity, dim=1, keepdim=True) > 0.5
        if torch.sum(bce_masks) < 1:
            return None, None, None

        batch_size = x_intensity.shape[0]

        bce_target = torch.masked_select(target_intensity[:, :-1], bce_masks)
        bce_weight = torch.ones_like(bce_target)
        bce_weight[bce_target == 0] = self.background_weight
        ce_loss = torch.nn.functional.binary_cross_entropy_with_logits(
            torch.masked_select(x_intensity, bce_masks),
            bce_target,
            weight=bce_weight,
        )

        reg_losses = [None for _ in target_regs]
        reg_masks = target_intensity[:, :-1] > 0.5
        if torch.sum(reg_masks) > 0:
            weight = None
            if self.multiplicity_correction:
                assert len(target_regs) == 2
                lengths = torch.norm(target_regs[0] - target_regs[1], dim=2)
                multiplicity = (lengths - 3.0) / self.independence_scale
                multiplicity = torch.clamp(multiplicity, min=1.0)
                multiplicity = torch.masked_select(multiplicity, reg_masks)
                weight = 1.0 / multiplicity

            reg_losses = []
            for i, (x_reg, x_spread, target_reg) in enumerate(zip(x_regs, x_spreads, target_regs)):
                if hasattr(self.regression_loss, 'scale'):
                    assert self.scales_to_kp is not None
                    self.regression_loss.scale = torch.masked_select(
                        torch.clamp(target_scale * self.scales_to_kp[i], 0.1, 1000.0),  # pylint: disable=unsubscriptable-object
                        reg_masks,
                    )

                reg_losses.append(self.regression_loss(
                    torch.masked_select(x_reg[:, :, 0], reg_masks),
                    torch.masked_select(x_reg[:, :, 1], reg_masks),
                    torch.masked_select(x_spread, reg_masks),
                    torch.masked_select(target_reg[:, :, 0], reg_masks),
                    torch.masked_select(target_reg[:, :, 1], reg_masks),
                    weight=weight,
                ) / 1000.0 / batch_size)

        scale_losses = []
        if x_scales:
            scale_losses = [
                torch.nn.functional.l1_loss(
                    torch.masked_select(x_scale, reg_masks),
                    torch.masked_select(target_scale * scale_to_kp, reg_masks),
                    reduction='sum',
                ) / 1000.0 / batch_size
                for x_scale, scale_to_kp in zip(x_scales, self.scales_to_kp)
            ]

        return [ce_loss] + reg_losses + scale_losses

class CrmLoss(Loss, torch.nn.Module):

    def __init__(self, head_name, regression_loss, *,
                 n_vectors=None, n_scales=None, sigmas=None):
        super(CrmLoss, self).__init__()

    @staticmethod
    def match(head_name):
        return head_name in (
            'crm',
        )
    @classmethod
    def cli(cls, parser):
        return
    @classmethod
    def apply_args(cls, args):
        return

    def forward(self, x, t):  # pylint: disable=arguments-differ
                  
        activity_loss = torch.nn.functional.mse_loss(x[0], t[0], reduction='sum') / x[0].shape[0]
        return [activity_loss]

def cli(parser):
    group = parser.add_argument_group('losses')
    group.add_argument('--r-smooth', type=float, default=0.0,
                       help='r_{smooth} for SmoothL1 regressions')
    group.add_argument('--regression-loss', default='laplace',
                       choices=['smoothl1', 'smootherl1', 'l1', 'laplace'],
                       help='type of regression loss')
    group.add_argument('--background-weight', default=1.0, type=float,
                       help='BCE weight of background')
    group.add_argument('--paf-multiplicity-correction',
                       default=False, action='store_true',
                       help='use multiplicity correction for PAF loss')
    group.add_argument('--paf-independence-scale', default=3.0, type=float,
                       help='linear length scale of independence for PAF regression')


def factory_from_args(args):
    for loss in Loss.__subclasses__():
        loss.apply_args(args)

    return factory(
        args.headnets,
        args.lambdas,
        args.regression_loss,
        args.r_smooth,
    ).to(device=args.device)


def factory(head_names, lambdas, reg_loss_name=None, r_smooth=None):
    head_names = [h for h in head_names if h not in ('skeleton',)]

    if reg_loss_name == 'smoothl1':
        reg_loss = SmoothL1Loss(r_smooth)
    elif reg_loss_name == 'l1':
        reg_loss = l1_loss
    elif reg_loss_name == 'laplace':
        reg_loss = laplace_loss
    elif reg_loss_name is None:
        reg_loss = laplace_loss
    else:
        raise Exception('unknown regression loss type {}'.format(reg_loss_name))

    losses = [factory_loss(head_name, reg_loss) for head_name in head_names]
    return MultiHeadLoss(losses, lambdas)


def factory_loss(head_name, reg_loss):
    for loss in Loss.__subclasses__():
        logging.debug('checking whether loss %s matches %s',
                      loss.__name__, head_name)
        if not loss.match(head_name):
            continue
        logging.info('selected loss %s for %s', loss.__name__, head_name)
        return loss(head_name, reg_loss)

    raise Exception('unknown headname {} for loss'.format(head_name))
