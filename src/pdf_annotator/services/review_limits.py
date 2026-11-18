"""Application-wide Responses admission and conservative rate feedback."""

from dataclasses import dataclass, field
from time import time

from .review_transport import TransportResult, duration, retry_after


@dataclass(frozen=True)
class RateLimits:
    rpm: int | None = None
    tpm: int | None = None

    def __post_init__(self) -> None:
        for value in (self.rpm, self.tpm):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("Rate limits must be positive integers")


@dataclass(eq=False)
class Reservation:
    project: str
    family: str
    started: float
    tokens: int
    active: bool = True


@dataclass
class Meter:
    limit: int | None = None
    remaining: int | None = None
    reset: float = 0.0
    last_reset: float = float("-inf")

    def constrain(self, limit: int | None) -> None:
        if limit is not None:
            self.limit = limit if self.limit is None else min(self.limit, limit)

    def expire(self, now: float) -> None:
        if self.remaining is not None and now >= self.reset:
            self.last_reset = self.reset
            self.remaining = None

    def observe(self, headers: dict[str, str], resource: str, now: float,
                started: float, outstanding: int) -> None:
        self.expire(now)
        limit = headers.get(f"x-ratelimit-limit-{resource}", "")
        if limit.isascii() and limit.isdecimal() and len(limit) <= 15:
            self.constrain(int(limit))
        remaining = headers.get(f"x-ratelimit-remaining-{resource}", "")
        reset = duration(headers.get(f"x-ratelimit-reset-{resource}", ""))
        if (started < self.last_reset or not remaining.isascii() or not remaining.isdecimal()
                or len(remaining) > 15):
            return
        value = max(0, int(remaining) - outstanding)
        # Concurrent snapshots can describe overlapping server state. They may
        # reduce the available budget, but never refill the current window.
        if self.remaining is None:
            self.remaining = value
        else:
            self.remaining = min(self.remaining, value)
        self.reset = max(self.reset, now + (60.0 if reset is None else reset))


@dataclass
class Budget:
    requests: Meter = field(default_factory=Meter)
    tokens: Meter = field(default_factory=Meter)
    charges: list[Reservation] = field(default_factory=list)
    cooldown: float = 0.0
    spacing: float = 0.0
    next_send: float = 0.0
    blocked: str | None = None

    def expire(self, now: float) -> None:
        self.charges[:] = [charge for charge in self.charges if charge.active or charge.started + 60 > now]
        self.requests.expire(now)
        self.tokens.expire(now)

    def wait(self, tokens: int, now: float) -> float:
        delay = max(0.0, self.cooldown - now, self.next_send - now)
        for meter, amount, is_tokens in ((self.requests, 1, False), (self.tokens, tokens, True)):
            if meter.remaining is not None and meter.remaining < amount:
                delay = max(delay, meter.reset - now)
            if meter.limit is None:
                continue
            used = sum(charge.tokens if is_tokens else 1 for charge in self.charges)
            for charge in self.charges:
                if used + amount <= meter.limit:
                    break
                # Once all expired in-flight charges settle, a new check can
                # make progress. Keep them charged in the meantime.
                delay = max(delay, charge.started + 60 - now, 0.05 if charge.active else 0.0)
                used -= charge.tokens if is_tokens else 1
        return delay


class RateController:
    """Used by the single scheduler thread. Project keys are opaque, in-memory IDs."""

    def __init__(self) -> None:
        self.projects: dict[str, Budget] = {}
        self.models: dict[tuple[str, str], Budget] = {}
        self.active: set[Reservation] = set()
        self.page_estimates: dict[tuple[str, str], int] = {}

    def budgets(self, project: str, family: str) -> tuple[Budget, Budget]:
        return (self.projects.setdefault(project, Budget()),
                self.models.setdefault((project, family), Budget()))

    def configure(self, project: str, family: str, project_limits: RateLimits,
                  model_limits: RateLimits) -> None:
        for budget, limits in zip(self.budgets(project, family), (project_limits, model_limits)):
            budget.requests.constrain(limits.rpm)
            budget.tokens.constrain(limits.tpm)

    def estimate(self, project: str, family: str, pages: int, base: int, per_page: int) -> int:
        return base + pages * max(per_page, self.page_estimates.get((project, family), 0))

    def token_limit(self, project: str, family: str) -> int | None:
        return min((b.tokens.limit for b in self.budgets(project, family)
                    if b.tokens.limit is not None), default=None)

    def budget_delay(self, project: str, family: str, tokens: int, now: float) -> float:
        return max(budget.wait(tokens, now) for budget in self.budgets(project, family))

    def availability(self, project: str, family: str, tokens: int,
                     now: float, *, check_global_slots: bool = True) -> tuple[float, str | None]:
        budgets = self.budgets(project, family)
        for budget in budgets:
            budget.expire(now)
            if budget.blocked:
                return 0.0, budget.blocked
            if budget.requests.limit == 0 or (budget.tokens.limit is not None and tokens > budget.tokens.limit):
                return 0.0, "batch_exceeds_rate_budget"
        has_rpm = any(b.requests.limit is not None or b.requests.remaining is not None for b in budgets)
        has_tpm = any(b.tokens.limit is not None or b.tokens.remaining is not None for b in budgets)
        project_active = any(item.project == project for item in self.active)
        if (check_global_slots and len(self.active) >= 2) or (project_active and not (has_rpm and has_tpm)):
            return 0.05, None
        return max(budget.wait(tokens, now) for budget in budgets), None

    def reserve(self, project: str, family: str, tokens: int, now: float) -> Reservation:
        if self.availability(project, family, tokens, now) != (0.0, None):
            raise RuntimeError("Responses admission without capacity")
        reservation = Reservation(project, family, now, tokens)
        self.active.add(reservation)
        project_budget, model_budget = self.budgets(project, family)
        for budget in (project_budget, model_budget):
            budget.charges.append(reservation)
            for meter, amount in ((budget.requests, 1), (budget.tokens, tokens)):
                if meter.remaining is not None:
                    meter.remaining -= amount
            budget.next_send = now + budget.spacing
        if not any(b.requests.limit is not None or b.requests.remaining is not None
                   for b in (project_budget, model_budget)):
            project_budget.next_send = max(project_budget.next_send, now + 1)
        return reservation

    def finish(self, reservation: Reservation, result: TransportResult, usage: dict,
               pages: int, now: float, backoff: float, wall_time=time) -> float:
        self.active.remove(reservation)
        reservation.active = False
        project, model = self.budgets(reservation.project, reservation.family)
        if not result.sent:
            project.charges.remove(reservation)
            model.charges.remove(reservation)
            # A failure after reservation may race feedback from another request.
            # Leave observed counters conservative for this window.
            return now
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
        actual = max(usage.get("total_tokens") or 0, (inputs or 0) + (outputs or 0))
        extra = max(0, actual - reservation.tokens)
        if extra:
            key = reservation.project, reservation.family
            estimate = (max(actual, inputs or 0) + pages - 1) // pages
            self.page_estimates[key] = max(self.page_estimates.get(key, 0), estimate)
            reservation.tokens += extra
        for budget, prefix in ((project, "project-"), (model, "")):
            budget.expire(now)
            if extra and budget.tokens.remaining is not None:
                budget.tokens.remaining = max(0, budget.tokens.remaining - extra)
            outstanding = [charge for charge in budget.charges if charge.active]
            budget.requests.observe(result.headers, prefix + "requests", now, reservation.started, len(outstanding))
            budget.tokens.observe(result.headers, prefix + "tokens", now, reservation.started,
                                  sum(charge.tokens for charge in outstanding))
        if result.systemic:
            if result.error in ("authentication_failed", "quota_exhausted", "permission_denied"):
                project.blocked = result.error
            elif result.error == "model_unavailable":
                model.blocked = result.error
        if result.error is None:
            project.spacing *= 0.8
            model.spacing *= 0.8
            return now
        if not result.retryable:
            return now
        delay = max(backoff, retry_after(result.headers.get("retry-after", ""), wall_time()) or 0)
        for resource in ("requests", "tokens", "project-requests", "project-tokens"):
            reset = duration(result.headers.get(f"x-ratelimit-reset-{resource}", "")) or 0
            remaining = result.headers.get(f"x-ratelimit-remaining-{resource}", "")
            needed = reservation.tokens if resource.endswith("tokens") else 1
            if remaining.isascii() and remaining.isdecimal() and len(remaining) <= 15:
                if int(remaining) < needed:
                    delay = max(delay, reset)
            elif result.status == 429:
                delay = max(delay, reset)
        scope = project if result.status == 429 else model
        if result.error == "slow_down":
            scope.spacing = min(60.0, max(1.0, scope.spacing * 2))
            delay = max(delay, scope.spacing)
        scope.cooldown = max(scope.cooldown, now + delay)
        return scope.cooldown
