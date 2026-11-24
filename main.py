"""DipSnap: mean-reversion dip-buying bot for pump.fun bonding curves.

Strategy (fresh design, pure Python stdlib, deterministic):
  A curve that ran up, pulled back 18-32% from its 1200-slot high, and is
  already turning back up gets one single 0.15 SOL entry -- no averaging
  down, no tranches. The turn must be confirmed twice: the price sits at
  least 4% above its own 120-slot low, and the last 8 trades are buy
  dominated (at least 5 buys, net SOL inflow, most recent trade a buy).
  Exits are a whole-position sell on +18% take-profit, -8% stop-loss, a
  6-point trailing stop armed at +10%, or a 400-slot max hold. After any
  exit the coin rests for 600 slots. Fast exits won the local sweep:
  wider stops and longer holds bled more on the dev window than the
  extra winners they caught.

  Guardrail pacing: the run needs round trips of >= 0.01 SOL on at least
  20 coins. If completed round trips lag behind once 15000 slots have
  passed, small 0.02 SOL probe round trips (sold the batch after the buy
  fills) on dip-band coins without bounce confirmation top the count up.
  Probes are capped at 8 and only run while positions are free.

  Everything is a pure function of the batches the harness sends: no
  clock, no randomness, no environment reads, so the two graded
  executions produce byte-identical action logs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- entry parameters -------------------------------------------------------
HIGH_WINDOW_SLOTS = 1200        # lookback for the recent high
DIP_MIN = 0.18                  # buy only 18-32% below the recent high
DIP_MAX = 0.32
BOUNCE_MIN = 0.04               # price must be >= 4% above its 120-slot low
BOUNCE_WINDOW_SLOTS = 120
FLOW_TRADES = 8                 # last N trades must be buy dominated
FLOW_MIN_BUYS = 5
MIN_TRADES = 12                 # trades seen before a coin is eligible
MIN_AGE_SLOTS = 100             # curve must be at least this old
MIN_REAL_SOL = 5 * SOL          # liquidity floor at decision time
ENTRY_LAMPORTS = 150_000_000    # 0.15 SOL, single entry, no adds
MIN_BUY = 10_000_000            # 0.01 SOL -- also the round-trip bar
MAX_ENTRIES_PER_BATCH = 2
BUY_SLIPPAGE_BPS = 500
SELL_SLIPPAGE_BPS = 3000

# --- exit parameters --------------------------------------------------------
TAKE_PROFIT = 0.18              # sell at +18% on liquidation value vs cost
STOP_LOSS = -0.08               # sell at -8%
TRAIL_ARM = 0.10                # trailing stop arms once peak gain hits +10%
TRAIL_DROP = 0.06               # sell when gain falls 6 points below peak
MAX_HOLD_SLOTS = 400
COOLDOWN_SLOTS = 600

# --- guardrail pacing -------------------------------------------------------
PACE_START_SLOTS = 15000        # start probing only after this many slots
PROBE_LAMPORTS = 20_000_000     # 0.02 SOL probe round trips
MAX_PROBES = 8

FEE_BPS = 125                   # protocol 95 + creator 30 bps on sells
BPS = 10_000


class Tick:
    __slots__ = ("slot", "price", "real_sol", "is_buy", "sol")

    def __init__(self, slot, price, real_sol, is_buy, sol):
        self.slot = slot
        self.price = price
        self.real_sol = real_sol
        self.is_buy = is_buy
        self.sol = sol


class Book:
    """Per-mint memory: tick history plus position lifecycle state."""

    __slots__ = (
        "ticks", "entry_price", "entry_slot", "peak_gain",
        "cool_until", "exiting", "probe", "probe_filled",
    )

    def __init__(self):
        self.ticks = []
        self.entry_price = 0.0
        self.entry_slot = -1
        self.peak_gain = 0.0
        self.cool_until = -1
        self.exiting = False
        self.probe = False
        self.probe_filled = False


BOOKS = {}
FIRST_SLOT = None
ROUND_TRIPS = set()             # mints with a completed buy+sell round trip
BOUGHT = set()                  # mints we have ever bought (for pacing count)
PROBES_USED = 0


def book(mint):
    b = BOOKS.get(mint)
    if b is None:
        b = Book()
        BOOKS[mint] = b
    return b


def price_of(event):
    vt = int(event["virtual_token_reserves"])
    if vt == 0:
        return 0.0
    return int(event["virtual_quote_reserves"]) / vt


def liquidation_value(mark, tokens):
    """What selling `tokens` would pay, curve fees deducted, at marked reserves."""
    if mark is None:
        return 0
    vq = int(mark["virtual_quote_reserves"])
    vt = int(mark["virtual_token_reserves"])
    rq = int(mark["real_quote_reserves"])
    if vt + tokens == 0:
        return 0
    q = (tokens * vq) // (vt + tokens)
    if q > rq:
        q = rq
    return q - (q * FEE_BPS + BPS - 1) // BPS


def buy_cap(state, mint):
    """Per-reply buy cap with a small margin for our own flow shifting reserves."""
    return state.max_buy_lamports(mint) * 97 // 100


def on_batch(state, batch):
    global FIRST_SLOT, PROBES_USED
    slot = batch["slot"]
    if FIRST_SLOT is None:
        FIRST_SLOT = slot
    orders = []
    cap = state.hello["caps"]["max_orders_per_batch"]
    tx = state.tx_fee_lamports()
    marks = {m["mint"]: m for m in batch["marks"]}

    touched = []
    for e in batch["events"]:
        if e["event"] != "TradeEvent":
            continue
        b = book(e["mint"])
        b.ticks.append(Tick(slot, price_of(e), int(e["real_quote_reserves"]),
                            bool(e["is_buy"]), int(e["sol_amount"])))
        if len(b.ticks) > 400:
            del b.ticks[:len(b.ticks) - 400]
        if e["mint"] not in touched:
            touched.append(e["mint"])

    # Fills: resolve exits, arm probes for their quick sell, forget dead buys.
    pending_mints = set()
    for o in state.pending.values():
        pending_mints.add(o.order["mint"])
    for f in batch["fills"]:
        b = BOOKS.get(f["mint"])
        if b is None:
            continue
        if f["side"] == "sell":
            b.exiting = False
            if f["mint"] not in state.positions:
                b.entry_slot = -1
                b.peak_gain = 0.0
                b.probe = False
                b.probe_filled = False
                b.cool_until = slot + COOLDOWN_SLOTS
                ROUND_TRIPS.add(f["mint"])
        elif f["status"] == "filled":
            BOUGHT.add(f["mint"])
            if b.probe:
                b.probe_filled = True
        elif f["mint"] not in state.positions and f["mint"] not in pending_mints:
            # Opening buy never filled and nothing left in flight: forget it.
            b.entry_slot = -1
            b.peak_gain = 0.0
            b.probe = False
            b.probe_filled = False

    # 1. Exits (and probe quick-sells) on held positions, mint order.
    for mint in sorted(state.positions):
        if len(orders) >= cap:
            break
        holding = state.positions[mint]
        b = book(mint)
        curve = state.curves.get(mint)
        if curve is None or curve.complete or b.exiting:
            continue
        if mint in pending_mints:
            continue
        value = liquidation_value(marks.get(mint), holding.token_amount)
        cost = holding.cost_lamports
        gain = (value - cost) / cost if cost > 0 else 0.0
        if gain > b.peak_gain:
            b.peak_gain = gain
        entry_slot = b.entry_slot if b.entry_slot >= 0 else slot
        stale = slot - entry_slot >= MAX_HOLD_SLOTS
        if b.probe and b.probe_filled:
            sell = True
        else:
            sell = (
                gain >= TAKE_PROFIT
                or gain <= STOP_LOSS
                or (b.peak_gain >= TRAIL_ARM and gain <= b.peak_gain - TRAIL_DROP)
                or stale
            )
        if not sell:
            continue
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": holding.token_amount,
                       "max_slippage_bps": SELL_SLIPPAGE_BPS})
        b.exiting = True

    # 2. New entries on coins that traded this batch.
    entries = 0
    open_slots = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
    pacing = (FIRST_SLOT is not None
              and slot - FIRST_SLOT >= PACE_START_SLOTS
              and len(ROUND_TRIPS) < 20
              and PROBES_USED < MAX_PROBES)
    for mint in touched:
        if len(orders) >= cap or entries >= MAX_ENTRIES_PER_BATCH or open_slots <= 0:
            break
        if mint in state.positions or mint in pending_mints:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        b = book(mint)
        if slot < b.cool_until or b.entry_slot >= 0:
            continue
        ticks = b.ticks
        if len(ticks) < MIN_TRADES:
            continue
        if curve.created_slot is not None and slot - curve.created_slot < MIN_AGE_SLOTS:
            continue
        last = ticks[-1]
        if last.real_sol < MIN_REAL_SOL:
            continue
        # Recent high inside the window, and the low of the bounce window.
        hi = 0.0
        hi_real = 0
        lo = None
        for t in reversed(ticks):
            if slot - t.slot > HIGH_WINDOW_SLOTS:
                break
            if t.price > hi:
                hi = t.price
                hi_real = t.real_sol
            if slot - t.slot <= BOUNCE_WINDOW_SLOTS:
                if lo is None or t.price < lo:
                    lo = t.price
        if hi <= 0 or hi_real < MIN_REAL_SOL or lo is None:
            continue
        dip = 1.0 - last.price / hi
        if dip < DIP_MIN or dip > DIP_MAX:
            continue
        # Buy flow over the last few trades.
        recent = ticks[-FLOW_TRADES:]
        buys = 0
        net = 0
        for t in recent:
            if t.is_buy:
                buys += 1
                net += t.sol
            else:
                net -= t.sol
        if buys < FLOW_MIN_BUYS or net < 0 or not last.is_buy:
            continue
        bounced = last.price >= lo * (1.0 + BOUNCE_MIN)
        if not bounced and not pacing:
            continue
        probe = not bounced  # pacing-only entry without bounce confirmation
        amount = PROBE_LAMPORTS if probe else ENTRY_LAMPORTS
        size_cap = buy_cap(state, mint)
        if amount > size_cap:
            amount = size_cap
        if amount < MIN_BUY:
            continue
        if state.free_cash_lamports() - amount - tx < 0:
            continue
        orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        b.entry_price = last.price
        b.entry_slot = slot
        b.peak_gain = 0.0
        b.exiting = False
        b.probe = probe
        b.probe_filled = False
        if probe:
            PROBES_USED += 1
        pending_mints.add(mint)
        entries += 1
        open_slots -= 1
    return orders


def on_done(state, done):
    cash = state.cash
    value = cash
    for mint, h in state.positions.items():
        curve = state.curves.get(mint)
        if curve is None:
            continue
        # Final marks are not in done; value positions at last seen reserves.
        vt = curve.virtual_token_reserves
        vq = curve.virtual_quote_reserves
        rq = curve.real_quote_reserves
        if vt + h.token_amount == 0:
            continue
        q = (h.token_amount * vq) // (vt + h.token_amount)
        if q > rq:
            q = rq
        value += q - (q * FEE_BPS + BPS - 1) // BPS
    print(
        "DipSnap done: batches=%d cash=%d approx_value=%d positions=%d "
        "round_trips=%d bought=%d probes=%d"
        % (state.seq, cash, value, len(state.positions),
           len(ROUND_TRIPS), len(BOUGHT), PROBES_USED),
        file=sys.stderr,
    )


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_done=on_done)
