from typing import Union, Tuple
from typing_extensions import Literal

import math

import eagerpy as ep

from ..models import Model

from ..criteria import Misclassification, TargetedMisclassification

from ..devutils import atleast_kd, flatten

from .base import MinimizationAttack
from .base import get_criterion
from .base import T


class EADAttack(MinimizationAttack):
    """EAD Attack with EN Decision Rule"""

    def __init__(
        self,
        binary_search_steps: int = 9,
        steps: int = 10000,
        initial_stepsize: float = 1e-2,
        confidence: float = 0.0,
        initial_const: float = 1e-3,
        regularization: float = 1e-2,
        decision_rule: Union[Literal["EN"], Literal["L1"]] = "EN",
        abort_early: bool = True,
    ):

        if decision_rule not in ("EN", "L1"):
            raise ValueError("invalid decision rule")

        self.binary_search_steps = binary_search_steps
        self.steps = steps
        self.confidence = confidence
        self.initial_stepsize = initial_stepsize
        self.regularization = regularization
        self.initial_const = initial_const
        self.abort_early = abort_early
        self.decision_rule = decision_rule

    def __call__(
        self,
        model: Model,
        inputs: T,
        criterion: Union[Misclassification, TargetedMisclassification, T],
    ) -> T:
        x, restore_type = ep.astensor_(inputs)
        criterion_ = get_criterion(criterion)
        del inputs, criterion

        N = len(x)

        if isinstance(criterion_, Misclassification):
            targeted = False
            classes = criterion_.labels
            change_classes_logits = self.confidence
        elif isinstance(criterion_, TargetedMisclassification):
            targeted = True
            classes = criterion_.target_classes
            change_classes_logits = -self.confidence
        else:
            raise ValueError("unsupported criterion")

        def is_adversarial(perturbed: ep.Tensor, logits: ep.Tensor) -> ep.Tensor:
            if change_classes_logits != 0:
                logits += ep.onehot_like(logits, classes, value=change_classes_logits)
            return criterion_(perturbed, logits)

        if classes.shape != (N,):
            name = "target_classes" if targeted else "labels"
            raise ValueError(
                f"expected {name} to have shape ({N},), got {classes.shape}"
            )

        min_, max_ = model.bounds
        rows = range(N)

        def loss_fun(y_k: ep.Tensor, consts: ep.Tensor) -> Tuple[ep.Tensor, ep.Tensor]:
            assert y_k.shape == x.shape
            assert consts.shape == (N,)

            logits = model(y_k)

            if targeted:
                c_minimize = best_other_classes(logits, classes)
                c_maximize = classes
            else:
                c_minimize = classes
                c_maximize = best_other_classes(logits, classes)

            is_adv_loss = logits[rows, c_minimize] - logits[rows, c_maximize]
            assert is_adv_loss.shape == (N,)

            is_adv_loss = is_adv_loss + self.confidence
            is_adv_loss = ep.maximum(0, is_adv_loss)
            is_adv_loss = is_adv_loss * consts

            squared_norms = flatten(y_k - x).square().sum(axis=-1)
            loss = is_adv_loss.sum() + squared_norms.sum()
            return loss, logits

        loss_aux_and_grad = ep.value_and_grad_fn(x, loss_fun, has_aux=True)

        consts = self.initial_const * ep.ones(x, (N,))
        lower_bounds = ep.zeros(x, (N,))
        upper_bounds = ep.inf * ep.ones(x, (N,))

        best_advs = ep.zeros_like(x)
        best_advs_norms = ep.ones(x, (N,)) * ep.inf

        # the binary search searches for the smallest consts that produce adversarials
        for binary_search_step in range(self.binary_search_steps):
            if (
                binary_search_step == self.binary_search_steps - 1
                and self.binary_search_steps >= 10
            ):
                # in the last iteration, repeat the search once
                consts = ep.minimum(upper_bounds, 1e10)

            # create a new optimizer find the delta that minimizes the loss
            x_k = x
            y_k = x

            found_advs = ep.full(
                x, (N,), value=False
            ).bool()  # found adv with the current consts
            loss_at_previous_check = ep.inf

            for iteration in range(self.steps):
                # square-root learning rate decay
                stepsize = self.initial_stepsize * (1.0 - iteration / self.steps) ** 0.5

                loss, logits, gradient = loss_aux_and_grad(y_k, consts)

                x_k_old = x_k
                x_k = project_shrinkage_thresholding(
                    y_k - stepsize * gradient, x, self.regularization, min_, max_
                )
                y_k = x_k + iteration / (iteration + 3.0) * (x_k - x_k_old)

                if self.abort_early and iteration % (math.ceil(self.steps / 10)) == 0:
                    # after each tenth of the iterations, check progress
                    if not loss.item() <= 0.9999 * loss_at_previous_check:
                        break  # stop optimization if there has been no progress
                    loss_at_previous_check = loss.item()

                found_advs_iter = is_adversarial(x_k, logits)

                best_advs, best_advs_norms = apply_decision_rule(
                    self.decision_rule,
                    self.regularization,
                    best_advs,
                    best_advs_norms,
                    x_k,
                    x,
                    found_advs_iter,
                )

                found_advs = ep.logical_or(found_advs, found_advs_iter)

            upper_bounds = ep.where(found_advs, consts, upper_bounds)
            lower_bounds = ep.where(found_advs, lower_bounds, consts)

            consts_exponential_search = consts * 10
            consts_binary_search = (lower_bounds + upper_bounds) / 2
            consts = ep.where(
                ep.isinf(upper_bounds), consts_exponential_search, consts_binary_search
            )

        return restore_type(best_advs)


def untargeted_is_adv(
    logits: ep.Tensor, labels: ep.Tensor, confidence: float
) -> ep.Tensor:
    logits = logits + ep.onehot_like(logits, labels, value=confidence)
    classes = logits.argmax(axis=-1)
    return classes != labels


def targeted_is_adv(
    logits: ep.Tensor, target_classes: ep.Tensor, confidence: float
) -> ep.Tensor:
    logits = logits - ep.onehot_like(logits, target_classes, value=confidence)
    classes = logits.argmax(axis=-1)
    return classes == target_classes


def best_other_classes(logits: ep.Tensor, exclude: ep.Tensor) -> ep.Tensor:
    other_logits = logits - ep.onehot_like(logits, exclude, value=ep.inf)
    return other_logits.argmax(axis=-1)


def apply_decision_rule(
    decision_rule: Union[Literal["EN"], Literal["L1"]],
    beta: float,
    best_advs: ep.Tensor,
    best_advs_norms: ep.Tensor,
    x_k: ep.Tensor,
    x: ep.Tensor,
    found_advs: ep.Tensor,
) -> Tuple[ep.Tensor, ep.Tensor]:
    if decision_rule == "EN":
        norms = beta * flatten(x_k - x).abs().sum(axis=-1) + flatten(
            x_k - x
        ).square().sum(axis=-1)
    elif decision_rule == "L1":
        norms = flatten(x_k - x).abs().sum(axis=-1)
    else:
        raise ValueError("invalid decision rule")

    new_best = ep.logical_and(norms < best_advs_norms, found_advs)
    new_best_kd = atleast_kd(new_best, best_advs.ndim)
    best_advs = ep.where(new_best_kd, x_k, best_advs)
    best_advs_norms = ep.where(new_best, norms, best_advs_norms)

    return best_advs, best_advs_norms


def project_shrinkage_thresholding(
    z: ep.Tensor, x0: ep.Tensor, regularization: float, min_: float, max_: float
) -> ep.Tensor:
    """Performs the element-wise projected shrinkage-thresholding
    operation"""

    upper_mask = (z - x0 > regularization).float32()
    lower_mask = (z - x0 < -regularization).float32()

    projection = (
        (1.0 - upper_mask - lower_mask) * x0
        + upper_mask * ep.minimum(z - regularization, max_)
        + lower_mask * ep.maximum(z + regularization, min_)
    )

    return projection
