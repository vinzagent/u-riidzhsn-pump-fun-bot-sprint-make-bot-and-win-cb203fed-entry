"""Steady Tide: a conservative pump.fun replay trading bot.

Protocol: agent-fun/replay/v2 (newline-delimited JSON on stdin/stdout).
The grader runs ``python3 -P -s main.py`` with ``PYTHONHASHSEED=0``.
Standard library only; no network, no clock, no randomness: every run is
deterministic, so the two graded executions produce identical action logs.

Strategy in one paragraph: wait for a coin to prove it has real, broad
buying interest (several distinct buyers, net SOL flowing in, price rising
towards its recent high, flow accelerating), then take a small probe
position and only add to it while it is winning. Every position carries a
hard stop-loss, a take-profit, a trailing stop once it is up, a fade exit
when flow turns against it and a maximum hold time. A tiny probe sleeve
keeps the run above the competition's guardrail (at least 20 coins with a
round trip of at least 0.01 SOL) without ever risking meaningful capital.
No martingale, no leverage, no shorts, no sniping of fresh launches.

The bot speaks the replay protocol directly: hello, one batch at a time,
done. Each batch carries the slot, the events (CreateEvent, TradeEvent,
CompleteEvent), marks (latest reserves per curve), fills for our orders
and the account. The bot answers with exactly one reply per batch.
"""

import json
import math
import re
import sys

PROTOCOL = "agent-fun/replay/v2"
LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000
#: Curve fee on a sale: protocol 95 bps + creator 30 bps = 1.25%.
CURVE_FEE_NUM = 125
CURVE_FEE_DEN = 10_000
MAX_HARNESS_LINE_BYTES = 1_048_576
MAX_BOT_LINE_BYTES = 65_536

_MINT_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9-][A-Za-z0-9_-]{0,63}")
_U64_RE = re.compile(r"0|[1-9][0-9]{0,19}")

#: Tuned on the kit's dev window; every value is a plain, round number on
#: purpose (no overfitting to a single coin or a single cell of a grid).
PARAMS = {
    # Entry signal (window in slots; ~4.7 slots per second on this chain).
    "window_slots": 30,
    "min_age_slots": 200,       # ignore fresh launches: mostly bundled dumps
    "min_net_sol": 0.75,        # net SOL (buys minus sells) over the window
    "min_buys": 4,
    "min_buyers": 4,            # distinct buying wallets in the window
    "min_price_up": 0.05,       # price rise over the window
    "max_price_up": 0.60,       # skip coins that already ran too far
    "near_high": 0.90,          # price now at least 90% of the window high
    "max_buyer_share": 0.60,    # largest single buyer at most 60% of buy SOL
    "min_real_sol": 1.5,        # real SOL reserves at entry
    "max_real_sol": 80.0,
    # Sizing (SOL).
    "initial_sol": 0.05,        # first buy: a probe, not a bet
    "add_sol": 0.10,            # each pyramid add
    "max_stake_sol": 0.50,      # most SOL at cost in one coin
    "min_buy_sol": 0.01,        # also the guardrail's minimum round trip
    "max_exposure_sol": 4.0,    # most SOL at cost across all positions
    "buy_slippage_bps": 2000,
    "sell_slippage_bps": 10_000,
    # Exits.
    "stop_loss": 0.09,          # sell when value falls 9% under cost
    "take_profit": 0.18,        # sell whole when value is 18% over cost
    "trail_arm": 0.10,          # trailing stop arms once up 10%
    "trail_drop": 0.10,         # ...then sell on a 10% fall from the peak
    "fade_window_slots": 20,
    "fade_net_sol": -0.30,      # sell when short-window flow turns negative
    "big_sell_sol": 2.0,        # one recorded sell this big exits the coin
    "max_hold_slots": 400,      # time limit, waived while up 15%+
    "hold_if_up": 0.15,
    "cooldown_slots": 200,      # wait after an exit before re-entering a coin
    "max_entries_per_coin": 2,
    # Guardrail pacing: the run must end with >= 20 coins traded.
    "target_coins": 25,
    "target_fills": 120,
    "pace_seconds": 21_600,     # the final window is 6 hours
    "probe_sol": 0.02,          # probe round trip, sold as soon as it lands
    "max_probes": 1,
}


class ProtocolError(Exception):
    """The harness sent something this bot cannot follow."""


class OrderError(Exception):
    """The bot tried to send something the harness would reject."""


def _describe(value):
    if isinstance(value, str):
        return json.dumps(value if len(value) <= 80 else value[:77] + "...")
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return "a %s" % type(value).__name__


def _check_order(order, where):
    """Validate one order the way the harness would; returns a wire order."""
    if not isinstance(order, dict):
        raise OrderError("%s: expected an order dict, got %s" % (where, _describe(order)))
    side = order.get("side")
    if side not in ("buy", "sell"):
        raise OrderError('%s: side must be "buy" or "sell", got %s' % (where, _describe(side)))
    allowed = ("side", "client_id", "mint", "quote_lamports", "max_slippage_bps")
    if side == "sell":
        allowed = ("side", "client_id", "mint", "token_amount", "max_slippage_bps")
    for key in order:
        if key not in allowed:
            raise OrderError("%s: unknown key %s on a %s order" % (where, _describe(key), side))
    mint = order.get("mint")
    if not isinstance(mint, str) or not _MINT_RE.fullmatch(mint):
        raise OrderError("%s: mint must be a base58 address, got %s" % (where, _describe(mint)))
    amount_key = "quote_lamports" if side == "buy" else "token_amount"
    amount = order.get(amount_key)
    if type(amount) is not int or amount < 0 or amount > 2**64 - 1:
        raise OrderError("%s: %s must be a u64 int, got %s" % (where, amount_key, _describe(amount)))
    bps = order.get("max_slippage_bps")
    if type(bps) is not int or not 0 <= bps <= 10_000:
        raise OrderError("%s: max_slippage_bps must be an int 0..10000, got %s" % (where, _describe(bps)))
    cid = order.get("client_id")
    if not isinstance(cid, str) or not _CLIENT_ID_RE.fullmatch(cid):
        raise OrderError("%s: bad client_id %s" % (where, _describe(cid)))
    wire = {"side": side, "client_id": cid, "mint": mint,
            amount_key: str(amount), "max_slippage_bps": bps}
    return wire


def _check_batch(batch, line_no):
    account = batch.get("account")
    ok = (
        type(batch.get("seq")) is int
        and type(batch.get("slot")) is int
        and isinstance(batch.get("events"), list)
        and isinstance(batch.get("marks"), list)
        and isinstance(batch.get("fills"), list)
        and isinstance(account, dict)
        and isinstance(account.get("positions"), list)
    )
    if not ok:
        raise ProtocolError("input line %d: malformed batch" % line_no)


class Curve:
    """A bonding curve at its latest known reserves (all ints, lamports)."""

    __slots__ = ("mint", "created_slot", "vq", "vt", "rq", "rt",
                 "reserves_slot", "trades", "complete")

    def __init__(self, mint, created_slot, slot):
        self.mint = mint
        #: Slot of its CreateEvent; None when first seen through a trade/mark.
        self.created_slot = created_slot
        self.vq = 0  # virtual quote (SOL) reserves
        self.vt = 0  # virtual token reserves
        self.rq = 0  # real quote (SOL) reserves
        self.rt = 0  # real token reserves
        #: Slot of the trade the reserves follow.
        self.reserves_slot = slot
        self.trades = 0
        self.complete = False


class Tape:
    """A curve's recent trade tape, kept for the entry signal."""

    __slots__ = ("created", "creator", "dev_sold", "trades", "entries", "last_exit")

    def __init__(self):
        self.created = None  # slot of the CreateEvent, None if unseen
        self.creator = None  # wallet that created the coin
        #: The creator sold at least once: a classic rug tell.
        self.dev_sold = False
        #: (slot, is_buy, sol, user, price) in arrival order.
        self.trades = []
        self.entries = 0     # entries taken on this coin so far
        self.last_exit = -10**18


class Held:
    """Bookkeeping for one open position."""

    __slots__ = ("opened", "peak", "last_add_price", "exiting", "filled")

    def __init__(self, opened, last_add_price):
        self.opened = opened
        #: Best close value seen, in SOL.
        self.peak = 0.0
        #: Recorded price when the last buy was decided.
        self.last_add_price = last_add_price
        self.exiting = False
        #: At least one buy of this position filled.
        self.filled = False


class Bot:
    """Everything the bot knows, kept current before each batch decision."""

    def __init__(self, hello):
        self.hello = hello
        self.seq = -1
        self.slot = -1
        self.cash = int(hello["capital"]["lamports"])
        #: mint -> (token_amount, cost_lamports), both ints.
        self.positions = {}
        #: Every curve seen, by mint, in first-seen order.
        self.curves = {}
        self.tapes = {}
        #: Orders sent and not yet reported in a fill, by client_id.
        self.pending = {}
        self.held = {}
        #: Probe round trips in flight, by mint: False while buying, True selling.
        self.probes = {}
        self.my_fills = 0
        self.my_coins = set()
        self.first_time = None
        self.last_time = None
        self._next_id = 1
        self.batches = 0

    # -- account helpers -------------------------------------------------
    def tx_fee_lamports(self):
        fees = self.hello["fees"]
        return int(fees["base_fee_lamports"]) + int(fees["priority_fee_lamports"])

    def max_buy_lamports(self, mint):
        """Most this batch's buys on one mint may spend together."""
        curve = self.curves.get(mint)
        if curve is None or curve.complete:
            return 0
        return curve.rq * int(self.hello["caps"]["max_order_bps_of_real_reserve"]) // BPS

    def committed_position_count(self):
        mints = set(self.positions)
        for order in self.pending.values():
            if order["side"] == "buy":
                mints.add(order["mint"])
        return len(mints)

    def free_cash_lamports(self):
        free = self.cash
        tx = self.tx_fee_lamports()
        for order in self.pending.values():
            free -= tx
            if order["side"] == "buy":
                free -= int(order["quote_lamports"])
        return free

    def _new_client_id(self):
        cid = "o%d" % self._next_id
        self._next_id += 1
        return cid

    # -- market math ------------------------------------------------------
    def price(self, mint):
        curve = self.curves.get(mint)
        if curve is None or curve.vt <= 0:
            return 0.0
        return curve.vq / curve.vt

    def position_value_lamports(self, mint, tokens):
        """What selling `tokens` now would net, after curve fee and one tx fee."""
        curve = self.curves.get(mint)
        if curve is None or curve.vt <= 0 or tokens <= 0:
            return 0
        proceeds = tokens * curve.vq // (curve.vt + tokens)
        net = proceeds * (BPS - CURVE_FEE_NUM) // BPS
        net -= self.tx_fee_lamports()
        return net if net > 0 else 0

    def net_flow(self, mint, now, window_slots):
        """Net recorded SOL (buys minus sells) over the last window."""
        tape = self.tapes.get(mint)
        if tape is None:
            return 0.0
        net = 0.0
        for slot, is_buy, sol, _user, _price in reversed(tape.trades):
            if slot <= now - window_slots:
                break
            net += sol if is_buy else -sol
        return net

    # -- state updates ----------------------------------------------------
    def _tape_of(self, mint):
        tape = self.tapes.get(mint)
        if tape is None:
            tape = Tape()
            self.tapes[mint] = tape
        return tape

    def _curve_of(self, mint, created_slot, slot):
        curve = self.curves.get(mint)
        if curve is None:
            curve = Curve(mint, created_slot, slot)
            self.curves[mint] = curve
        return curve

    def _set_account(self, account):
        self.cash = int(account["cash_lamports"])
        positions = {}
        for pos in account["positions"]:
            tokens = int(pos["token_amount"])
            if tokens > 0:
                positions[pos["mint"]] = (tokens, int(pos["cost_lamports"]))
        self.positions = positions

    def apply_batch(self, batch):
        """Fold a batch in: its fills, account, marks and events, in that order."""
        self.seq = batch["seq"]
        self.slot = batch["slot"]
        for fill in batch["fills"]:
            self.pending.pop(fill["client_id"], None)
            if fill["status"] == "filled" and not fill["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins.add(fill["mint"])
                held = self.held.get(fill["mint"])
                if held is not None and fill["side"] == "buy":
                    held.filled = True
        self._set_account(batch["account"])
        for mark in batch["marks"]:
            mint = mark["mint"]
            slot = int(mark["slot"])
            curve = self.curves.get(mint)
            if curve is not None and slot < curve.reserves_slot:
                continue
            if curve is None:
                curve = self._curve_of(mint, None, slot)
            curve.vq = int(mark["virtual_quote_reserves"])
            curve.vt = int(mark["virtual_token_reserves"])
            curve.rq = int(mark["real_quote_reserves"])
            curve.rt = int(mark["real_token_reserves"])
            curve.reserves_slot = slot
        for event in batch["events"]:
            kind = event["event"]
            mint = event["mint"]
            slot = int(event["slot"])
            if event.get("block_time") is not None:
                stamp = int(event["block_time"])
                if self.first_time is None:
                    self.first_time = stamp
                if self.last_time is None or stamp > self.last_time:
                    self.last_time = stamp
            if kind == "CreateEvent":
                curve = self._curve_of(mint, slot, slot)
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = 0  # a create carries no real quote reserves
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = slot
                tape = self._tape_of(mint)
                tape.created = slot
                tape.creator = event["creator"]
            elif kind == "TradeEvent":
                curve = self._curve_of(mint, None, slot)
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = int(event["real_quote_reserves"])
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = slot
                curve.trades += 1
                price = (int(event["virtual_quote_reserves"])
                         / int(event["virtual_token_reserves"]))
                tape = self._tape_of(mint)
                if not event["is_buy"] and tape.creator is not None \
                        and event["user"] == tape.creator:
                    tape.dev_sold = True
                tape.trades.append(
                    (slot, bool(event["is_buy"]),
                     int(event["sol_amount"]) / LAMPORTS_PER_SOL,
                     event["user"], price))
            else:  # CompleteEvent
                self._curve_of(mint, None, slot).complete = True

    # -- strategy ---------------------------------------------------------
    def entry_signal(self, tape, now, rq_sol):
        """True when a curve's tape shows broad, accelerating buy pressure."""
        p = PARAMS
        if tape.dev_sold:
            return False
        if rq_sol < p["min_real_sol"] or rq_sol > p["max_real_sol"]:
            return False
        if tape.created is None or now - tape.created < p["min_age_slots"]:
            return False
        start = now - p["window_slots"]
        mid = now - p["window_slots"] // 2
        buy_sol = 0.0
        sell_sol = 0.0
        buys = 0
        net_old = 0.0
        net_new = 0.0
        start_price = -1.0
        high = 0.0
        last = 0.0
        by_user = {}
        for slot, is_buy, sol, user, price in tape.trades:
            if slot <= start:
                start_price = price
                continue
            if start_price < 0:
                start_price = price
            signed = sol if is_buy else -sol
            if slot <= mid:
                net_old += signed
            else:
                net_new += signed
            if is_buy:
                buy_sol += sol
                buys += 1
                by_user[user] = by_user.get(user, 0.0) + sol
            else:
                sell_sol += sol
            if price > high:
                high = price
            last = price
        if buys < p["min_buys"] or len(by_user) < p["min_buyers"]:
            return False
        if buy_sol - sell_sol < p["min_net_sol"]:
            return False
        if start_price <= 0 or last <= 0:
            return False
        up = last / start_price - 1.0
        if up < p["min_price_up"] or up > p["max_price_up"]:
            return False
        if last < high * p["near_high"]:
            return False
        if net_new <= 0 or net_new < net_old:
            return False
        top = 0.0
        for value in by_user.values():
            if value > top:
                top = value
        if top > p["max_buyer_share"] * buy_sol:
            return False
        return True

    def on_batch(self, batch):
        """Decide this batch's orders. Exits first, then entries, then probes."""
        p = PARAMS
        now = batch["slot"]
        cap_orders = int(self.hello["caps"]["max_orders_per_batch"])
        tx = self.tx_fee_lamports()
        orders = []

        # Mints that traded this batch, in event order (never a set: the
        # decision order must be identical in both graded executions).
        traded = []
        seen = set()
        big_sell = set()
        for event in batch["events"]:
            mint = event["mint"]
            if mint not in seen:
                seen.add(mint)
                traded.append(mint)
            if (event["event"] == "TradeEvent" and not event["is_buy"]
                    and int(event["sol_amount"]) / LAMPORTS_PER_SOL >= p["big_sell_sol"]):
                big_sell.add(mint)

        # Prune the tapes of coins that traded, to the lookback we still need.
        keep = max(p["window_slots"], p["fade_window_slots"]) + 10
        for mint in traded:
            tape = self.tapes.get(mint)
            if tape is None:
                continue
            cut = 0
            while cut < len(tape.trades) and tape.trades[cut][0] <= now - keep:
                cut += 1
            if cut:
                del tape.trades[:cut]

        pending_sides = {}
        for order in self.pending.values():
            pending_sides.setdefault(order["mint"], set()).add(order["side"])

        # 1. Exits: stop-loss, take-profit, trailing stop, fade, big sell,
        #    and the hold-time limit. A position sells whole, never in part.
        for mint in list(self.held):
            pos = self.positions.get(mint)
            held = self.held[mint]
            if pos is None:
                if mint not in pending_sides:
                    if held.filled:
                        self._tape_of(mint).last_exit = now
                    else:
                        tape = self.tapes.get(mint)
                        if tape is not None and tape.entries > 0:
                            tape.entries -= 1
                    del self.held[mint]
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            tokens, cost = pos
            cost_sol = cost / LAMPORTS_PER_SOL
            value_sol = self.position_value_lamports(mint, tokens) / LAMPORTS_PER_SOL
            if value_sol > held.peak:
                held.peak = value_sol
            leave = held.exiting
            if not leave and value_sol < cost_sol * (1 - p["stop_loss"]):
                leave = True
            if not leave and value_sol >= cost_sol * (1 + p["take_profit"]):
                leave = True
            if (not leave and held.peak >= cost_sol * (1 + p["trail_arm"])
                    and value_sol < held.peak * (1 - p["trail_drop"])):
                leave = True
            if not leave and self.net_flow(mint, now, p["fade_window_slots"]) < p["fade_net_sol"]:
                leave = True
            if not leave and mint in big_sell:
                leave = True
            tape = self.tapes.get(mint)
            if not leave and tape is not None and tape.dev_sold:
                leave = True
            if (not leave and now - held.opened > p["max_hold_slots"]
                    and value_sol < cost_sol * (1 + p["hold_if_up"])):
                leave = True
            if not leave:
                continue
            held.exiting = True
            if "sell" in pending_sides.get(mint, ()):
                continue
            if len(orders) >= cap_orders:
                break
            orders.append({"side": "sell", "mint": mint, "token_amount": tokens,
                           "max_slippage_bps": p["sell_slippage_bps"],
                           "client_id": self._new_client_id()})

        # 2. Probe round trips: sell a probe as soon as its buy has landed.
        for mint in list(self.probes):
            selling = self.probes[mint]
            pos = self.positions.get(mint)
            if not selling:
                if pos is not None:
                    self.probes[mint] = True
                elif mint not in pending_sides:
                    del self.probes[mint]  # the probe buy never landed
                continue
            if pos is None:
                if mint not in pending_sides:
                    del self.probes[mint]
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete or "sell" in pending_sides.get(mint, ()):
                continue
            if len(orders) >= cap_orders:
                break
            orders.append({"side": "sell", "mint": mint, "token_amount": pos[0],
                           "max_slippage_bps": p["sell_slippage_bps"],
                           "client_id": self._new_client_id()})

        # 3. Entries and pyramid adds, in the order the coins traded.
        free = self.free_cash_lamports()
        slots_left = int(self.hello["caps"]["max_open_positions"]) - self.committed_position_count()
        exposure = 0.0
        for _mint, (_tokens, cost) in self.positions.items():
            exposure += cost / LAMPORTS_PER_SOL
        for order in self.pending.values():
            if order["side"] == "buy":
                exposure += int(order["quote_lamports"]) / LAMPORTS_PER_SOL
        for mint in traded:
            if len(orders) >= cap_orders:
                break
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_sides or mint in self.probes:
                continue
            tape = self.tapes.get(mint)
            if tape is None:
                continue
            held = self.held.get(mint)
            price = self.price(mint)
            rq_sol = curve.rq / LAMPORTS_PER_SOL
            if held is None:
                if slots_left <= 0:
                    continue
                if tape.entries >= p["max_entries_per_coin"]:
                    continue
                if now - tape.last_exit < p["cooldown_slots"]:
                    continue
                if not self.entry_signal(tape, now, rq_sol):
                    continue
                want = p["initial_sol"]
            else:
                if held.exiting or not held.filled:
                    continue
                pos = self.positions.get(mint)
                if pos is None:
                    continue
                cost_sol = pos[1] / LAMPORTS_PER_SOL
                if cost_sol >= p["max_stake_sol"] - p["min_buy_sol"]:
                    continue
                if price < held.last_add_price * 1.02:
                    continue
                value_sol = self.position_value_lamports(mint, pos[0]) / LAMPORTS_PER_SOL
                if value_sol < cost_sol * 1.10:
                    continue
                if self.net_flow(mint, now, p["window_slots"]) < 0.25:
                    continue
                if rq_sol > p["max_real_sol"]:
                    continue
                want = min(p["add_sol"], p["max_stake_sol"] - cost_sol)
            cap_sol = self.max_buy_lamports(mint) / LAMPORTS_PER_SOL
            amount = min(cap_sol, want, p["max_exposure_sol"] - exposure)
            room = (free - tx) / LAMPORTS_PER_SOL
            if amount > room:
                amount = room
            if amount < p["min_buy_sol"]:
                continue
            lamports = int(amount * LAMPORTS_PER_SOL)
            if lamports < int(p["min_buy_sol"] * LAMPORTS_PER_SOL):
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                           "max_slippage_bps": p["buy_slippage_bps"],
                           "client_id": self._new_client_id()})
            free -= lamports + tx
            exposure += amount
            if held is None:
                self.held[mint] = Held(opened=now, last_add_price=price)
                tape.entries += 1
                slots_left -= 1
            else:
                held.last_add_price = price

        # 4. Guardrail pacing: open a tiny probe round trip when the run's
        #    own fills or coins lag the pace set in block time.
        if (len(self.probes) < p["max_probes"] and slots_left > 0
                and self.first_time is not None and self.last_time is not None):
            pace = min(1.0, (self.last_time - self.first_time) / p["pace_seconds"])
            need_coins = len(self.my_coins) < math.ceil(p["target_coins"] * pace)
            need_fills = self.my_fills < math.ceil(p["target_fills"] * pace)
            if need_coins or need_fills:
                min_rq = (p["probe_sol"]
                          / (int(self.hello["caps"]["max_order_bps_of_real_reserve"]) / BPS)
                          * 1.5)
                for mint in traded:
                    if len(orders) >= cap_orders or len(self.probes) >= p["max_probes"]:
                        break
                    if (mint in self.held or mint in self.probes
                            or mint in pending_sides or mint in self.positions):
                        continue
                    if need_coins and not need_fills and mint in self.my_coins:
                        continue
                    curve = self.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.rq / LAMPORTS_PER_SOL < min_rq:
                        continue
                    if free < int(p["probe_sol"] * LAMPORTS_PER_SOL) + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(p["probe_sol"] * LAMPORTS_PER_SOL),
                                   "max_slippage_bps": p["buy_slippage_bps"],
                                   "client_id": self._new_client_id()})
                    self.probes[mint] = False
                    free -= int(p["probe_sol"] * LAMPORTS_PER_SOL) + tx
                    slots_left -= 1
        return orders

    def apply_done(self, done):
        self.seq = done["seq"]
        self._set_account(done["account"])

    def summary(self):
        start = int(self.hello["capital"]["lamports"])
        value = self.cash
        for mint, (tokens, _cost) in self.positions.items():
            value += self.position_value_lamports(mint, tokens)
        pnl_sol = (value - start) / LAMPORTS_PER_SOL
        return ("batches=%d fills=%d coins=%d cash=%.6f SOL positions=%d "
                "net_pnl=%.6f SOL" % (self.batches, self.my_fills,
                                      len(self.my_coins),
                                      self.cash / LAMPORTS_PER_SOL,
                                      len(self.positions), pnl_sol))


# -- the wire ---------------------------------------------------------------

def run():
    """Speak agent-fun/replay/v2 on stdin/stdout until done."""
    bot = None
    line_no = 0
    for raw in sys.stdin.buffer:
        line_no += 1
        if len(raw) > MAX_HARNESS_LINE_BYTES + 1:
            raise ProtocolError("input line %d is over %d bytes"
                                % (line_no, MAX_HARNESS_LINE_BYTES))
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError("input line %d is not JSON" % line_no) from None
        if not isinstance(msg, dict):
            raise ProtocolError("input line %d: not an object" % line_no)
        kind = msg.get("type")
        if kind == "hello":
            if bot is not None:
                raise ProtocolError("input line %d: a second hello" % line_no)
            if msg.get("protocol") != PROTOCOL:
                raise ProtocolError("the harness speaks %s; this bot speaks %s"
                                    % (_describe(msg.get("protocol")), PROTOCOL))
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                raise ProtocolError("input line %d: a batch before hello" % line_no)
            _check_batch(msg, line_no)
            bot.apply_batch(msg)
            orders = [_check_order(o, "batch %d order %d" % (msg["seq"], i))
                      for i, o in enumerate(bot.on_batch(msg))]
            cap = int(bot.hello["caps"]["max_orders_per_batch"])
            if len(orders) > cap:
                raise OrderError("batch %d: %d orders, over the cap of %d"
                                 % (msg["seq"], len(orders), cap))
            for order in orders:
                bot.pending[order["client_id"]] = order
            text = json.dumps({"type": "reply", "seq": msg["seq"], "orders": orders},
                              separators=(",", ":"), ensure_ascii=False)
            if len(text.encode("utf-8")) > MAX_BOT_LINE_BYTES:
                raise OrderError("batch %d: the reply is over %d bytes"
                                 % (msg["seq"], MAX_BOT_LINE_BYTES))
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            bot.batches += 1
        elif kind == "done":
            if bot is None:
                raise ProtocolError("input line %d: done before hello" % line_no)
            bot.apply_done(msg)
            sys.stderr.write("[steady-tide] done: %s\n" % bot.summary())
            return 0
        else:
            raise ProtocolError("input line %d: unknown message type %s"
                                % (line_no, _describe(kind)))
    if bot is None:
        raise ProtocolError("input ended before hello")
    raise ProtocolError("input ended before done (after %d batches)" % bot.batches)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (ProtocolError, OrderError) as exc:
        sys.stderr.write("[steady-tide] error: %s\n" % exc)
        sys.exit(1)
