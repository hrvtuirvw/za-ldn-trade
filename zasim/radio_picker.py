"""Selection-only local Wi-Fi inventory and worker-side read-only preflight.

A displayed interface is not a compatibility certificate. No automatic device
selection, privilege escalation, relay startup, scan, key read or radio mutation.
Names are bound to sysfs object identities, never to a combobox index.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
import threading

from .control import raise_if_cancelled
from .i18n import tr
from .text import display_safe
from .wireless import inventory_radios, inspect_wireless, _small

SYSFS = Path('/sys/class/net')
_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z')


@dataclass(frozen=True)
class RadioChoice:
    interface: str
    phy: str
    driver: str
    identity: tuple[int, ...]
    product: str = ''
    state: str = 'unchecked'

    @property
    def key(self) -> tuple:
        # Product text and current busy status may change without changing the
        # selected hardware. Interface/PHY/driver or sysfs replacement may not.
        return self.interface, self.phy, self.driver, self.identity

    def label(self) -> str:
        name = self.product or self.driver
        state = {'unchecked': tr('未診断'), 'in_use': tr('使用中'),
                 'blocked': tr('無効化中')}[self.state]
        return f'{display_safe(name, limit=64)} — {self.interface} [{state}]'


@dataclass(frozen=True)
class RadioInventory:
    status: str
    radios: tuple[RadioChoice, ...] = ()


def _identity(entry: Path) -> tuple[int, ...]:
    result = []
    for path in (entry, entry / 'phy80211', entry / 'device'):
        stat = path.stat()
        result.extend((stat.st_dev, stat.st_ino))
    return tuple(result)


def read_inventory(*, sysfs: Path | None = None, platform: str | None = None) -> RadioInventory:
    """Fast sysfs reads only. Failure is unknown, not proof of no adapters."""
    if not (sys.platform if platform is None else platform).startswith('linux'):
        return RadioInventory('not_linux')
    sysfs = SYSFS if sysfs is None else Path(sysfs)
    try:
        rows = inventory_radios(sysfs)
        radios = []
        for row in rows:
            if not all(_NAME.fullmatch(row[key]) for key in ('interface', 'phy', 'driver')):
                raise ValueError('invalid inventory name')
            entry = sysfs / row['interface']
            before = _identity(entry)
            product = ''
            device = (entry / 'device').resolve(strict=True)
            # USB interfaces expose the product on their immediate parent.
            # No recursive walking and no serial numbers/MACs are collected.
            for path in (device / 'product', device.parent / 'product'):
                product = _small(path) or ''
                if product:
                    break
            busy = _small(entry / 'carrier') == '1'
            blocked = any(_small(rf / kind) == '1'
                          for rf in (entry / 'phy80211').glob('rfkill*')
                          for kind in ('soft', 'hard'))
            if before != _identity(entry) or (entry / 'phy80211').resolve().name != row['phy']:
                raise ValueError('inventory changed')
            radios.append(RadioChoice(row['interface'], row['phy'], row['driver'], before,
                                      product, 'blocked' if blocked else 'in_use' if busy else 'unchecked'))
        if rows != inventory_radios(sysfs):
            raise ValueError('inventory changed')
        # A sibling netdev uses the SAME physical radio: don't label its other
        # interface free just because that netdev's carrier is down.
        states = {radio.phy: 'unchecked' for radio in radios}
        for state in ('in_use', 'blocked'):
            for radio in radios:
                if radio.state == state:
                    states[radio.phy] = state
        from dataclasses import replace
        return RadioInventory('ok', tuple(replace(r, state=states[r.phy]) for r in radios))
    except (OSError, ValueError, KeyError, TypeError):
        return RadioInventory('unknown')


def resolve_choice(choice: RadioChoice | None, inventory: RadioInventory) -> RadioChoice:
    if inventory.status != 'ok':
        raise ValueError(tr('このPCのWi-Fi一覧を取得できません。「一覧を更新」で確認してください。'))
    if choice is None:
        raise ValueError(tr('使うWi-Fiアダプターを一覧から選んでください。'))
    matches = [r for r in inventory.radios if r.key == choice.key]
    if len(matches) != 1:
        raise ValueError(tr('選んだWi-Fiが外れたか変更されています。一覧を更新して選び直してください。'))
    current = matches[0]
    if current.state in ('in_use', 'blocked'):
        raise ValueError(tr('このWi-Fiは使用中または無効です。接続を自動で切らず、別の専用機を選んでください。'))
    return current


def preflight_choice(choice: RadioChoice, *, cancel_event: threading.Event | None = None,
                     ifname: str | None = None, reuse: bool | None = None) -> None:
    """Run in the existing operation worker, before any transport/key access.

    Fail closed on missing features, routes/IP/carrier, unknown state or hotplug.
    This point-in-time read is not a radio lease or a hardware success claim;
    the transport's ownership/restoration guards still apply independently.
    """
    raise_if_cancelled(cancel_event)
    snapshot = read_inventory()
    selected = resolve_choice(choice, snapshot)
    if ifname is not None:
        if not _NAME.fullmatch(ifname) or len(ifname.encode('utf-8')) > 15:
            raise ValueError(tr('一時インターフェイス名が不正です。詳細設定を確認してください。'))
        if reuse is True:
            if ifname != selected.interface or sum(r.phy == selected.phy for r in snapshot.radios) != 1:
                raise ValueError(tr('この無線機の既存インターフェイスを一意に再利用できません。詳細設定を確認してください。'))
        elif (SYSFS / ifname).exists():
            raise ValueError(tr('一時インターフェイス名は既に使われています。詳細設定で別名を指定してください。'))
    report = inspect_wireless(radios=[{'interface': r.interface, 'phy': r.phy,
                                      'driver': r.driver} for r in snapshot.radios],
                              phyname=selected.phy, sysfs=SYSFS)
    raise_if_cancelled(cancel_event)
    current = read_inventory()
    resolve_choice(choice, current)
    if ({r.key for r in snapshot.radios if r.phy == choice.phy}
            != {r.key for r in current.radios if r.phy == choice.phy}):
        raise ValueError(tr('選んだWi-Fiが外れたか変更されています。一覧を更新して選び直してください。'))
    if ifname is not None and reuse is not True and (SYSFS / ifname).exists():
        raise ValueError(tr('一時インターフェイス名は既に使われています。詳細設定で別名を指定してください。'))
    if (report['selected_phy'] != choice.phy
            or report['status'] != 'requirements_present_not_verified'):
        raise ValueError(tr('選んだWi-Fiの必要条件を確認できません。「環境チェック」で詳細を確認してください。'))
