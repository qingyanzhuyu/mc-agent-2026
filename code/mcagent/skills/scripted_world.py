"""Explicit responses for offline skill contract tests.

A response is a dict, an exception instance, or a callable accepting
`(world, **request)`. Only the test installs any side effects.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from copy import deepcopy

from .testing import GridWorld

Response = dict | Exception | Callable[..., dict]


class ScriptedWorld(GridWorld):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._replies: dict[str, deque[Response]] = {}

    def queue(self, method: str, *responses: Response) -> None:
        self._replies.setdefault(method, deque()).extend(responses)

    def assert_exhausted(self) -> None:
        unused = {method: len(queue) for method, queue in self._replies.items() if queue}
        if unused:
            raise AssertionError(f"夹具仍有未消费的回执: {unused}")

    def _reply(self, method: str, request: dict, *, fallback=None) -> dict:
        if method not in self._replies and fallback is not None:
            return fallback(**request)
        self.calls.append((method, deepcopy(request)))
        replies = self._replies.get(method)
        if not replies:
            raise AssertionError(f"夹具未安排 {method} 的回执: {request}")
        response = replies.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(self, **request)
        return deepcopy(response)

    def status(self, timeout=10):
        return self._reply("status", {"timeout": timeout}, fallback=super().status)

    def inventory(self, timeout=10):
        return self._reply("inventory", {"timeout": timeout}, fallback=super().inventory)

    def get_block(self, pos):
        return self._reply("get_block", {"pos": pos}, fallback=super().get_block)

    def events(self, since=0, limit=100):
        return self._reply("events", {"since": since, "limit": limit}, fallback=super().events)

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        return self._reply("find_blocks", dict(types=types, radius=radius, limit=limit,
                                               properties=properties, mature=mature))

    def get_entities(self, radius=32, types=None):
        return self._reply("get_entities", dict(radius=radius, types=types))

    def advancements(self, status="completed", include_recipes=False):
        return self._reply("advancements", dict(status=status, include_recipes=include_recipes))

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        return self._reply("move_to", dict(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos,
                                          timeout=timeout, stop_on=stop_on), fallback=super().move_to)

    def dig(self, pos, collect=True, *, stop_on=None):
        return self._reply("dig", dict(pos=pos, collect=collect, stop_on=stop_on), fallback=super().dig)

    def place(self, item, pos=None, *, stop_on=None):
        return self._reply("place", dict(item=item, pos=pos, stop_on=stop_on), fallback=super().place)

    def equip(self, item, slot="hand", *, stop_on=None):
        return self._reply("equip", dict(item=item, slot=slot, stop_on=stop_on), fallback=super().equip)

    def posture(self, sneak=None, shield=None, *, jump=None):
        req = dict(sneak=sneak, shield=shield)
        if jump is not None:
            req["jump"] = jump
        return self._reply("posture", req, fallback=super().posture)

    def stop(self):
        return self._reply("stop", {}, fallback=super().stop)

    def craft(self, item, count=1, *, stop_on=None):
        return self._reply("craft", dict(item=item, count=count, stop_on=stop_on))

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        return self._reply("interact", dict(pos=pos, entity_id=entity_id, item=item, stop_on=stop_on))

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
        return self._reply("use_item", dict(item=item, target=target, ms=ms, timeout_ms=timeout_ms,
                                           source=source, stop_on=stop_on))

    def attack(self, entity_id, timeout_ms=20000, *, stop_on=None):
        return self._reply("attack", dict(entity_id=entity_id, timeout_ms=timeout_ms, stop_on=stop_on))

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None):
        return self._reply("shoot", dict(entity_id=entity_id, pos=pos, weapon=weapon, charge_ms=charge_ms,
                                         stop_on=stop_on))

    def window_view(self, target, *, stop_on=None):
        return self._reply("window_view", dict(target=target, stop_on=stop_on))

    def window_act(self, target, ops, *, stop_on=None):
        return self._reply("window_act", dict(target=target, ops=ops, stop_on=stop_on))
