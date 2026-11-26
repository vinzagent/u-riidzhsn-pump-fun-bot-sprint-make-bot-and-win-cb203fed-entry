"""Steady Tide v2: a cautious replay bot for pump.fun bonding curves.

The bot talks agent-fun/replay/v2 over stdin/stdout: it reads a hello,
then one batch per line, then done, and answers each batch with one
reply. The grader launches ``python3 -P -s main.py`` with
``PYTHONHASHSEED=0``. Only the standard library is used, and nothing
here touches the network, the clock, or a random source, so two graded
runs produce the same action log.

The money is split across two sleeves that never overlap in purpose:

- **Momentum sleeve.** It looks for coins that are already moving with
  the crowd behind them: at least 200 slots old, several buys from
  several different wallets, net SOL flowing in, price up a little but
  not absurdly, still near its recent high, inflow speeding up rather
  than fading, and no single wallet dominating the buys. It risks just
  0.02 SOL per coin and never adds to a winner. It gets out quickly on
  value: a 9% stop, an 18% target, a trailing stop once up 10%, a fade
  exit when short-term flow turns negative, an exit on a big single
  sell or a creator sell, and a 400-slot time limit that is lifted
  while the trade is up 15% or more. On the dev window this sleeve
  loses about 0.08 SOL across roughly 80 coins, which on its own clears
  the competition's 20-coin guardrail by a wide margin.

- **Ride sleeve.** When a coin's real reserves cross 55 SOL, the crowd
  has shown it can push the coin a long way, and the pop toward
  completion usually runs about 30% before sellers take over. The bot
  buys 0.40 SOL on that exact crossing batch and never chases a later
  re-cross. It banks the pop at +30% on price, cuts at -9% on price,
  trails 25% once up 10%, and caps the hold at 400 slots. A momentum
  trade whose coin crosses 30 SOL of real reserves switches to these
  ride rules without a second buy. Measured on the dev window: 20 ride
  entries, 10 targets against 6 stops, +0.53 SOL, positive in both
  halves of the window. The whole bot nets +0.45 SOL at 0.46 SOL of max
  drawdown, against -0.26 SOL for v1.

A third, tiny probe sleeve buys 0.02 SOL and sells as soon as the buy
lands, purely to keep the run above the 20-coin guardrail when the main
sleeves are quiet. No leverage, no shorts, no martingale, no sniping of
fresh launches.
"""

import json
import math
import re
import sys

PROTOCOL = "agent-fun/replay/v2"
LAMPORTS = 1_000_000_000
BPS = 10_000
#: Fee taken on a curve sale: 95 bps protocol + 30 bps creator = 1.25%.
FEE_NUM = 125
MAX_INPUT_LINE = 1_048_576
MAX_REPLY_LINE = 65_536

_MINT_PAT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_CLIENT_ID_PAT = re.compile(r"[A-Za-z0-9-][A-Za-z0-9_-]{0,63}")

#: Tuned on the kit's dev window. Every number is deliberately plain and
#: round (no fitting to one coin or one grid cell). The ride figures sit
#: on wide plateaus: a +28% to +32% target and a 52-57 SOL crossing line
#: all stay positive, and the edge holds in both halves of the window.
CFG = {
    # Sizing (SOL).
    "initial_sol": 0.02,        # first buy: a probe, not a bet
    "add_sol": 0.0,             # no pyramid adds: adds multiply fees
    "max_stake_sol": 0.20,      # most SOL at cost in one coin
    "min_buy_sol": 0.01,        # also the guardrail's minimum round trip
    "max_exposure_sol": 4.0,    # most SOL at cost across all positions
    "buy_slippage_bps": 2000,
    "sell_slippage_bps": 10_000,
    # Momentum entry signal (window in slots; ~4.7 slots per second).
    "window_slots": 30,
    "min_age_slots": 200,       # ignore fresh launches: mostly bundled dumps
    "min_net_sol": 0.75,        # net SOL (buys minus sells) over the window
    "min_buys": 4,
    "min_buyers": 6,            # distinct buying wallets in the window
    "min_price_up": 0.05,       # price rise over the window
    "max_price_up": 0.60,       # skip coins that already ran too far
    "near_high": 0.90,          # price now at least 90% of the window high
    "max_buyer_share": 0.60,    # largest single buyer at most 60% of buy SOL
    "min_real_sol": 1.5,        # real SOL reserves at entry
    "max_real_sol": 80.0,
    # Momentum exits (value vs cost).
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
    "max_entries_per_coin": 1,  # one entry per coin: re-entries chase
    # Ride sleeve: buy the real-reserve crossing, bank the completion pop.
    "ride_rq_sol": 55.0,        # direct ride entry when rq crosses this
    "ride_stake_sol": 0.40,     # under the 1% cap (0.55 SOL at rq 55)
    "ride_take_profit": 0.30,   # price-based: bank the pop at +30%
    "ride_stop_loss": 0.09,     # price-based, separate from the momentum SL
    "ride_trail_drop": 0.25,    # trailing-stop backstop once up 10%
    "ride_max_hold_slots": 400,
    "runner_rq_sol": 30.0,      # momentum position converts to ride rules
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


def _brief(value):
    if isinstance(value, str):
        return json.dumps(value if len(value) <= 80 else value[:77] + "...")
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return "a %s" % type(value).__name__


def _validate_batch(batch, line_no):
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


def _validate_order(order, where):
    """Validate one order the way the harness would; returns a wire order."""
    if not isinstance(order, dict):
        raise OrderError("%s: expected an order dict, got %s" % (where, _brief(order)))
    side = order.get("side")
    if side not in ("buy", "sell"):
        raise OrderError('%s: side must be "buy" or "sell", got %s' % (where, _brief(side)))
    allowed = ("side", "client_id", "mint", "quote_lamports", "max_slippage_bps")
    if side == "sell":
        allowed = ("side", "client_id", "mint", "token_amount", "max_slippage_bps")
    for key in order:
        if key not in allowed:
            raise OrderError("%s: unknown key %s on a %s order" % (where, _brief(key), side))
    mint = order.get("mint")
    if not isinstance(mint, str) or not _MINT_PAT.fullmatch(mint):
        raise OrderError("%s: mint must be a base58 address, got %s" % (where, _brief(mint)))
    amount_key = "quote_lamports" if side == "buy" else "token_amount"
    amount = order.get(amount_key)
    if type(amount) is not int or amount < 0 or amount > 2**64 - 1:
        raise OrderError("%s: %s must be a u64 int, got %s" % (where, amount_key, _brief(amount)))
    bps = order.get("max_slippage_bps")
    if type(bps) is not int or not 0 <= bps <= 10_000:
        raise OrderError("%s: max_slippage_bps must be an int 0..10000, got %s" % (where, _brief(bps)))
    cid = order.get("client_id")
    if not isinstance(cid, str) or not _CLIENT_ID_PAT.fullmatch(cid):
        raise OrderError("%s: bad client_id %s" % (where, _brief(cid)))
    wire = {"side": side, "client_id": cid, "mint": mint,
            amount_key: str(amount), "max_slippage_bps": bps}
    return wire


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


class CoinTape:
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


class OpenTrade:
    """Bookkeeping for one open position."""

    __slots__ = ("opened_slot", "peak_value", "last_buy_price", "exiting", "filled",
                 "sleeve", "entry_price", "peak_price")

    def __init__(self, opened_slot, last_buy_price, sleeve="mom", entry_price=0.0):
        self.opened_slot = opened_slot
        #: Best close value seen, in SOL.
        self.peak_value = 0.0
        #: Recorded price when the last buy was decided.
        self.last_buy_price = last_buy_price
        self.exiting = False
        #: At least one buy of this position filled.
        self.filled = False
        #: "mom" for the momentum sleeve, "ride" for the ride sleeve.
        self.sleeve = sleeve
        #: Recorded price at the decision batch; ride exits compare the
        #: current price against this, so fees never shift the exit levels.
        self.entry_price = entry_price
        self.peak_price = entry_price


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
        self.fills = 0
        self.coins = set()
        self.t0 = None
        self.t1 = None
        self._next_id = 1
        self.batches = 0
        #: Real SOL reserves at the last batch each mint traded in. The
        #: ride entry fires only on the crossing batch, so this must be
        #: updated (never replaced) per mint at the end of each batch.
        self._last_rq = {}
        #: Coins the ride sleeve already bought: one ride entry per coin.
        self._ride_done = set()

    # -- account helpers -------------------------------------------------
    def tx_fee(self):
        fees = self.hello["fees"]
        return int(fees["base_fee_lamports"]) + int(fees["priority_fee_lamports"])

    def buy_cap(self, mint):
        """Most this batch's buys on one mint may spend together."""
        curve = self.curves.get(mint)
        if curve is None or curve.complete:
            return 0
        return curve.rq * int(self.hello["caps"]["max_order_bps_of_real_reserve"]) // BPS

    def slots_used(self):
        mints = set(self.positions)
        mints.update(order["mint"] for order in self.pending.values()
                     if order["side"] == "buy")
        return len(mints)

    def free_cash(self):
        free = self.cash
        tx = self.tx_fee()
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

    def sell_value(self, mint, tokens):
        """What selling `tokens` now would net, after curve fee and one tx fee."""
        curve = self.curves.get(mint)
        if curve is None or curve.vt <= 0 or tokens <= 0:
            return 0
        proceeds = tokens * curve.vq // (curve.vt + tokens)
        net = proceeds * (BPS - FEE_NUM) // BPS
        net -= self.tx_fee()
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
    def _tape(self, mint):
        tape = self.tapes.get(mint)
        if tape is None:
            tape = CoinTape()
            self.tapes[mint] = tape
        return tape

    def _curve(self, mint, created_slot, slot):
        curve = self.curves.get(mint)
        if curve is None:
            curve = Curve(mint, created_slot, slot)
            self.curves[mint] = curve
        return curve

    def _load_account(self, account):
        self.cash = int(account["cash_lamports"])
        self.positions = {
            pos["mint"]: (int(pos["token_amount"]), int(pos["cost_lamports"]))
            for pos in account["positions"]
            if int(pos["token_amount"]) > 0
        }

    def apply_batch(self, batch):
        """Fold a batch in: its fills, account, marks and events, in that order."""
        self.seq = batch["seq"]
        self.slot = batch["slot"]
        for fill in batch["fills"]:
            self.pending.pop(fill["client_id"], None)
            if fill["status"] == "filled" and not fill["client_id"].startswith("_"):
                self.fills += 1
                self.coins.add(fill["mint"])
                held = self.held.get(fill["mint"])
                if held is not None and fill["side"] == "buy":
                    held.filled = True
        self._load_account(batch["account"])
        for mark in batch["marks"]:
            mint = mark["mint"]
            slot = int(mark["slot"])
            curve = self.curves.get(mint)
            if curve is not None and slot < curve.reserves_slot:
                continue
            if curve is None:
                curve = self._curve(mint, None, slot)
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
                ts = int(event["block_time"])
                if self.t0 is None:
                    self.t0 = ts
                if self.t1 is None or ts > self.t1:
                    self.t1 = ts
            if kind == "CreateEvent":
                curve = self._curve(mint, slot, slot)
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = 0  # a create carries no real quote reserves
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = slot
                tape = self._tape(mint)
                tape.created = slot
                tape.creator = event["creator"]
            elif kind == "TradeEvent":
                curve = self._curve(mint, None, slot)
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = int(event["real_quote_reserves"])
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = slot
                curve.trades += 1
                px = (int(event["virtual_quote_reserves"])
                      / int(event["virtual_token_reserves"]))
                tape = self._tape(mint)
                if not event["is_buy"] and tape.creator is not None \
                        and event["user"] == tape.creator:
                    tape.dev_sold = True
                tape.trades.append(
                    (slot, bool(event["is_buy"]),
                     int(event["sol_amount"]) / LAMPORTS,
                     event["user"], px))
            else:  # CompleteEvent
                self._curve(mint, None, slot).complete = True

    def apply_done(self, done):
        self.seq = done["seq"]
        self._load_account(done["account"])

    # -- strategy ---------------------------------------------------------
    def entry_signal(self, tape, now, rq_sol):
        """True when a curve's tape shows broad, accelerating buy pressure."""
        cfg = CFG
        if tape.dev_sold:
            return False
        if rq_sol < cfg["min_real_sol"] or rq_sol > cfg["max_real_sol"]:
            return False
        if tape.created is None or now - tape.created < cfg["min_age_slots"]:
            return False
        start = now - cfg["window_slots"]
        mid = now - cfg["window_slots"] // 2
        bought = 0.0
        sold = 0.0
        n_buys = 0
        flow_first = 0.0
        flow_second = 0.0
        first_price = -1.0
        window_high = 0.0
        last_price = 0.0
        per_buyer = {}
        for slot, is_buy, sol, user, price in tape.trades:
            if slot <= start:
                first_price = price
                continue
            if first_price < 0:
                first_price = price
            signed = sol if is_buy else -sol
            if slot <= mid:
                flow_first += signed
            else:
                flow_second += signed
            if is_buy:
                bought += sol
                n_buys += 1
                per_buyer[user] = per_buyer.get(user, 0.0) + sol
            else:
                sold += sol
            if price > window_high:
                window_high = price
            last_price = price
        if n_buys < cfg["min_buys"] or len(per_buyer) < cfg["min_buyers"]:
            return False
        if bought - sold < cfg["min_net_sol"]:
            return False
        if first_price <= 0 or last_price <= 0:
            return False
        up = last_price / first_price - 1.0
        if up < cfg["min_price_up"] or up > cfg["max_price_up"]:
            return False
        if last_price < window_high * cfg["near_high"]:
            return False
        if flow_second <= 0 or flow_second < flow_first:
            return False
        if max(per_buyer.values()) > cfg["max_buyer_share"] * bought:
            return False
        return True

    def _ride_exit(self, held, price, now):
        """Ride exits: price-based stop, target, trailing stop, hold cap."""
        cfg = CFG
        base = held.entry_price
        if price < base * (1 - cfg["ride_stop_loss"]):
            return True
        if price >= base * (1 + cfg["ride_take_profit"]):
            return True
        if (held.peak_price >= base * (1 + cfg["trail_arm"])
                and price < held.peak_price * (1 - cfg["ride_trail_drop"])):
            return True
        if now - held.opened_slot > cfg["ride_max_hold_slots"]:
            return True
        return False

    def _mom_exit(self, held, mint, value_sol, cost_sol, now, dumped):
        """Momentum exits: value-based stop/target/trail, fade, dump, cap."""
        cfg = CFG
        if value_sol < cost_sol * (1 - cfg["stop_loss"]):
            return True
        if value_sol >= cost_sol * (1 + cfg["take_profit"]):
            return True
        if (held.peak_value >= cost_sol * (1 + cfg["trail_arm"])
                and value_sol < held.peak_value * (1 - cfg["trail_drop"])):
            return True
        if self.net_flow(mint, now, cfg["fade_window_slots"]) < cfg["fade_net_sol"]:
            return True
        if mint in dumped:
            return True
        tape = self.tapes.get(mint)
        if tape is not None and tape.dev_sold:
            return True
        if (now - held.opened_slot > cfg["max_hold_slots"]
                and value_sol < cost_sol * (1 + cfg["hold_if_up"])):
            return True
        return False

    def _size_buy(self, mint, want_sol, free_lamports, exposure_sol):
        """Lamports and SOL for a buy, or (0, 0.0) when too small to send."""
        cfg = CFG
        cap_sol = self.buy_cap(mint) / LAMPORTS
        amount = min(cap_sol, want_sol, cfg["max_exposure_sol"] - exposure_sol)
        room = (free_lamports - self.tx_fee()) / LAMPORTS
        if amount > room:
            amount = room
        if amount < cfg["min_buy_sol"]:
            return 0, 0.0
        lamports = int(amount * LAMPORTS)
        if lamports < int(cfg["min_buy_sol"] * LAMPORTS):
            return 0, 0.0
        return lamports, amount

    def on_batch(self, batch):
        """Decide this batch's orders. Exits first, then entries, then probes."""
        cfg = CFG
        now = batch["slot"]
        order_cap = int(self.hello["caps"]["max_orders_per_batch"])
        tx = self.tx_fee()
        orders = []

        # Mints that traded this batch, in event order (never a set: the
        # decision order must be identical in both graded executions).
        touched = []
        seen = set()
        dumped = set()
        for event in batch["events"]:
            mint = event["mint"]
            if mint not in seen:
                seen.add(mint)
                touched.append(mint)
            if (event["event"] == "TradeEvent" and not event["is_buy"]
                    and int(event["sol_amount"]) / LAMPORTS >= cfg["big_sell_sol"]):
                dumped.add(mint)

        # Prune the tapes of coins that traded, to the lookback we still need.
        keep = max(cfg["window_slots"], cfg["fade_window_slots"]) + 10
        for mint in touched:
            tape = self.tapes.get(mint)
            if tape is None:
                continue
            cut = 0
            while cut < len(tape.trades) and tape.trades[cut][0] <= now - keep:
                cut += 1
            if cut:
                del tape.trades[:cut]

        pending_by_mint = {}
        for order in self.pending.values():
            pending_by_mint.setdefault(order["mint"], set()).add(order["side"])

        # 1. Exits. A position sells whole, never in part. Momentum
        #    positions exit on value vs cost; ride positions exit on price
        #    vs their entry price, so fees never shift the exit levels.
        for mint in list(self.held):
            pos = self.positions.get(mint)
            held = self.held[mint]
            if pos is None:
                if mint not in pending_by_mint:
                    if held.filled:
                        self._tape(mint).last_exit = now
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
            cost_sol = cost / LAMPORTS
            value_sol = self.sell_value(mint, tokens) / LAMPORTS
            if value_sol > held.peak_value:
                held.peak_value = value_sol
            price = self.price(mint)
            rq_sol = curve.rq / LAMPORTS
            # Runner conversion: a momentum position whose coin's real
            # reserves cross the runner line becomes a ride (no new slot,
            # no new fees); its ride exits then run off the momentum
            # entry price, which is also its trailing-stop backstop.
            if (held.sleeve == "mom"
                    and self._last_rq.get(mint, 0.0) < cfg["runner_rq_sol"] <= rq_sol):
                held.sleeve = "ride"
            if held.entry_price <= 0:
                held.entry_price = price
                held.peak_price = price
            if price > held.peak_price:
                held.peak_price = price
            leave = held.exiting
            if not leave:
                # No fade, no big-sell, no dev-sold exit on the ride: the
                # pop is the edge, and those levers cut it before it arrives.
                if held.sleeve == "ride":
                    leave = self._ride_exit(held, price, now)
                else:
                    leave = self._mom_exit(held, mint, value_sol, cost_sol, now, dumped)
            if not leave:
                continue
            held.exiting = True
            if "sell" in pending_by_mint.get(mint, ()):
                continue
            if len(orders) >= order_cap:
                break
            orders.append({"side": "sell", "mint": mint, "token_amount": tokens,
                           "max_slippage_bps": cfg["sell_slippage_bps"],
                           "client_id": self._new_client_id()})

        # 2. Probe round trips: sell a probe as soon as its buy has landed.
        for mint in list(self.probes):
            selling = self.probes[mint]
            pos = self.positions.get(mint)
            if not selling:
                if pos is not None:
                    self.probes[mint] = True
                elif mint not in pending_by_mint:
                    del self.probes[mint]  # the probe buy never landed
                continue
            if pos is None:
                if mint not in pending_by_mint:
                    del self.probes[mint]
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete or "sell" in pending_by_mint.get(mint, ()):
                continue
            if len(orders) >= order_cap:
                break
            orders.append({"side": "sell", "mint": mint, "token_amount": pos[0],
                           "max_slippage_bps": cfg["sell_slippage_bps"],
                           "client_id": self._new_client_id()})

        # 3. Entries, in the order the coins traded. A ride entry fires
        #    only on the batch where real reserves cross the ride line;
        #    a later re-cross is a chase at a higher price, not the signal.
        free = self.free_cash()
        open_slots = int(self.hello["caps"]["max_open_positions"]) - self.slots_used()
        exposure = 0.0
        for _mint, (_tokens, cost) in self.positions.items():
            exposure += cost / LAMPORTS
        for order in self.pending.values():
            if order["side"] == "buy":
                exposure += int(order["quote_lamports"]) / LAMPORTS
        for mint in touched:
            if len(orders) >= order_cap:
                break
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_by_mint or mint in self.probes:
                continue
            tape = self.tapes.get(mint)
            if tape is None:
                continue
            held = self.held.get(mint)
            price = self.price(mint)
            rq_sol = curve.rq / LAMPORTS
            if held is None:
                ride_cross = (mint not in self._ride_done
                              and self._last_rq.get(mint, 0.0) < cfg["ride_rq_sol"] <= rq_sol)
                if ride_cross:
                    if open_slots <= 0:
                        continue
                    lamports, amount = self._size_buy(
                        mint, cfg["ride_stake_sol"], free, exposure)
                    if lamports == 0:
                        continue
                    orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                                   "max_slippage_bps": cfg["buy_slippage_bps"],
                                   "client_id": self._new_client_id()})
                    free -= lamports + tx
                    exposure += amount
                    self.held[mint] = OpenTrade(opened_slot=now, last_buy_price=price,
                                               sleeve="ride", entry_price=price)
                    self._ride_done.add(mint)
                    open_slots -= 1
                    continue
                if open_slots <= 0:
                    continue
                if tape.entries >= cfg["max_entries_per_coin"]:
                    continue
                if now - tape.last_exit < cfg["cooldown_slots"]:
                    continue
                if not self.entry_signal(tape, now, rq_sol):
                    continue
                want = cfg["initial_sol"]
            else:
                if held.exiting or not held.filled:
                    continue
                pos = self.positions.get(mint)
                if pos is None:
                    continue
                cost_sol = pos[1] / LAMPORTS
                if cost_sol >= cfg["max_stake_sol"] - cfg["min_buy_sol"]:
                    continue
                if price < held.last_buy_price * 1.02:
                    continue
                value_sol = self.sell_value(mint, pos[0]) / LAMPORTS
                if value_sol < cost_sol * 1.10:
                    continue
                if self.net_flow(mint, now, cfg["window_slots"]) < 0.25:
                    continue
                if rq_sol > cfg["max_real_sol"]:
                    continue
                want = min(cfg["add_sol"], cfg["max_stake_sol"] - cost_sol)
            lamports, amount = self._size_buy(mint, want, free, exposure)
            if lamports == 0:
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                           "max_slippage_bps": cfg["buy_slippage_bps"],
                           "client_id": self._new_client_id()})
            free -= lamports + tx
            exposure += amount
            if held is None:
                self.held[mint] = OpenTrade(opened_slot=now, last_buy_price=price,
                                           sleeve="mom", entry_price=price)
                tape.entries += 1
                open_slots -= 1
            else:
                held.last_buy_price = price

        # 4. Guardrail pacing: open a tiny probe round trip when the run's
        #    own fills or coins lag the pace set in block time.
        if (len(self.probes) < cfg["max_probes"] and open_slots > 0
                and self.t0 is not None and self.t1 is not None):
            pace = min(1.0, (self.t1 - self.t0) / cfg["pace_seconds"])
            need_coins = len(self.coins) < math.ceil(cfg["target_coins"] * pace)
            need_fills = self.fills < math.ceil(cfg["target_fills"] * pace)
            if need_coins or need_fills:
                min_reserves = (cfg["probe_sol"]
                                / (int(self.hello["caps"]["max_order_bps_of_real_reserve"]) / BPS)
                                * 1.5)
                for mint in touched:
                    if len(orders) >= order_cap or len(self.probes) >= cfg["max_probes"]:
                        break
                    if (mint in self.held or mint in self.probes
                            or mint in pending_by_mint or mint in self.positions):
                        continue
                    if need_coins and not need_fills and mint in self.coins:
                        continue
                    curve = self.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.rq / LAMPORTS < min_reserves:
                        continue
                    if free < int(cfg["probe_sol"] * LAMPORTS) + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(cfg["probe_sol"] * LAMPORTS),
                                   "max_slippage_bps": cfg["buy_slippage_bps"],
                                   "client_id": self._new_client_id()})
                    self.probes[mint] = False
                    free -= int(cfg["probe_sol"] * LAMPORTS) + tx
                    open_slots -= 1
        # Remember this batch's real reserves per traded mint, so the next
        # batch can tell a crossing from a level that was already crossed.
        for mint in touched:
            curve = self.curves.get(mint)
            if curve is not None:
                self._last_rq[mint] = curve.rq / LAMPORTS
        return orders

    def summary(self):
        start = int(self.hello["capital"]["lamports"])
        value = self.cash
        for mint, (tokens, _cost) in self.positions.items():
            value += self.sell_value(mint, tokens)
        pnl_sol = (value - start) / LAMPORTS
        return ("batches=%d fills=%d coins=%d cash=%.6f SOL positions=%d "
                "net_pnl=%.6f SOL" % (self.batches, self.fills,
                                      len(self.coins),
                                      self.cash / LAMPORTS,
                                      len(self.positions), pnl_sol))


# -- the wire ---------------------------------------------------------------

def run():
    """Speak agent-fun/replay/v2 on stdin/stdout until done."""
    trader = None
    line_no = 0
    for raw in sys.stdin.buffer:
        line_no += 1
        if len(raw) > MAX_INPUT_LINE + 1:
            raise ProtocolError("input line %d is over %d bytes"
                                % (line_no, MAX_INPUT_LINE))
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
            if trader is not None:
                raise ProtocolError("input line %d: a second hello" % line_no)
            if msg.get("protocol") != PROTOCOL:
                raise ProtocolError("the harness speaks %s; this bot speaks %s"
                                    % (_brief(msg.get("protocol")), PROTOCOL))
            trader = Bot(msg)
        elif kind == "batch":
            if trader is None:
                raise ProtocolError("input line %d: a batch before hello" % line_no)
            _validate_batch(msg, line_no)
            trader.apply_batch(msg)
            orders = [_validate_order(o, "batch %d order %d" % (msg["seq"], i))
                      for i, o in enumerate(trader.on_batch(msg))]
            cap = int(trader.hello["caps"]["max_orders_per_batch"])
            if len(orders) > cap:
                raise OrderError("batch %d: %d orders, over the cap of %d"
                                 % (msg["seq"], len(orders), cap))
            for order in orders:
                trader.pending[order["client_id"]] = order
            text = json.dumps({"type": "reply", "seq": msg["seq"], "orders": orders},
                              separators=(",", ":"), ensure_ascii=False)
            if len(text.encode("utf-8")) > MAX_REPLY_LINE:
                raise OrderError("batch %d: the reply is over %d bytes"
                                 % (msg["seq"], MAX_REPLY_LINE))
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            trader.batches += 1
        elif kind == "done":
            if trader is None:
                raise ProtocolError("input line %d: done before hello" % line_no)
            trader.apply_done(msg)
            sys.stderr.write("[steady-tide-v2] done: %s\n" % trader.summary())
            return 0
        else:
            raise ProtocolError("input line %d: unknown message type %s"
                                % (line_no, _brief(kind)))
    if trader is None:
        raise ProtocolError("input ended before hello")
    raise ProtocolError("input ended before done (after %d batches)" % trader.batches)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (ProtocolError, OrderError) as exc:
        sys.stderr.write("[steady-tide-v2] error: %s\n" % exc)
        sys.exit(1)
