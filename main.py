"""Pullback Rider — a paper-trading bot for the pump.fun replay (agent-fun/replay/v2).

The bot speaks the replay protocol over stdin/stdout: the harness sends a
``hello`` line with the capital, caps, latency and fees, then one ``batch``
per slot window carrying the events, marks and fills for that slot, and the
bot answers every batch with one reply echoing its ``seq`` and the ``orders``
it wants placed. A final ``done`` line closes the run. Every number the
harness sends is a u64 decimal string; the bot keeps all of its own books
in lamports (ints) and prices as plain float ratios, so two runs of the same
replay produce the same action log byte for byte: no clock, no randomness,
no environment reads, and iteration only over insertion-ordered dicts and
the batch's own lists.

Strategy, in one paragraph: ride the second wind, not the launch. Every
coin is watched from its CreateEvent. A coin earns an entry only after it
has shown real early strength — it ran up at least 25% from its create
price, buyers outweigh sellers two-to-one on the tape, and the creator has
not sold — and then given some of it back in a pullback from its peak and
started to bounce off the dip low. That bounce is the entry: the launch
snipers already left, the tape has been tested once, and the price is
still well above where the coin started. The bot buys one stake per coin,
banks half at a first take-profit, and lets the rest run under a trailing
stop, with a hard stop, a hold limit and a stale exit behind it. A small
pacing probe keeps the run above the guardrail pace (at least 20 coins,
each with a round trip of 0.01 SOL or more) by opening tiny round trips on
liquid curves whenever the bot's own fills or coins lag the pace.

Paper money only: nothing here touches a real wallet, a real trade, or the
network. The grader runs it as ``python3 -P -s /bot/main.py`` with
``PYTHONHASHSEED=0``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL

# --- Pullback entry: what a coin's tape must show -------------------------
MIN_AGE_SLOTS = 3          # youngest age (slots since create) an entry may be decided at
MAX_AGE_SLOTS = 120        # oldest age (slots since create) an entry may be decided at
MIN_PEAK_RUNUP = 1.25      # peak price over the create price: the strength test
MIN_PULLBACK = 0.05        # price must have fallen this far from its peak: the dip
BOUNCE = 0.02              # ... and recovered this far from its dip low: the bounce
MIN_ABOVE_CREATE = 1.10    # ... while still holding this far above the create price
MIN_BUY_SELL_RATIO = 2.0   # cumulative buy SOL over cumulative sell SOL on the tape
MIN_REAL_SOL = 3.0         # curve real SOL bounds at decision time
MAX_REAL_SOL = 80.0
SKIP_DEV_SOLD = True       # creator sold before entry: skip the coin

# --- Sizing ----------------------------------------------------------------
STAKE_SOL = 0.08           # lamports to aim for per coin in total
MIN_BUY_SOL = 0.015        # smallest single buy worth paying the tx fee for
BUY_SLIPPAGE_BPS = 10000
SELL_SLIPPAGE_BPS = 10000

# --- Exits -----------------------------------------------------------------
TRIM_AT = 0.25             # bank half the position when value reaches cost * (1 + TRIM_AT)
TAKE_PROFIT = 0.60         # sell the rest when value reaches cost * (1 + TAKE_PROFIT)
STOP_LOSS = 0.10           # sell all when value falls to cost * (1 - STOP_LOSS)
TRAIL_ARM = 0.15           # trailing stop arms once peak value reaches cost * (1 + TRAIL_ARM)
TRAIL = 0.12               # sell all when value falls to peak * (1 - TRAIL)
MAX_HOLD_SLOTS = 120       # exit after this many slots held
STALE_SLOTS = 600          # exit when no trade has hit the coin for this many slots
EXIT_ON_DEV_SELL = True    # exit at once when the creator sells while held

# --- Guardrail pacing ------------------------------------------------------
TARGET_COINS = 21          # coins the run should reach (guardrail needs 20)
TARGET_FILLS = 42          # fills the run should reach (the harness sets no fill floor)
PACE_SECONDS = 21600       # block-time seconds the pace is measured over (6 h)
PROBE_SOL = 0.012          # probe buy size (above the 0.01 SOL round-trip floor)
MAX_PROBES = 1             # most probe round trips open at once

CURVE_FEE_BPS = 125        # protocol 95 bps + creator 30 bps, charged on every fill


def _lamports(sol):
    return int(sol * LAMPORTS_PER_SOL)


class Coin:
    """One watched launch, from its CreateEvent on."""

    __slots__ = ("created", "creator", "vq0", "vt0", "peak", "trough", "buy_sol",
                 "sell_sol", "dev_sold", "last_trade_slot", "done",
                 "entered", "complete")

    def __init__(self, created, creator, vq0, vt0):
        self.created = created
        self.creator = creator
        self.vq0 = vq0             # create-time reserves: run-up is measured against them
        self.vt0 = vt0
        self.peak = 1.0            # best price over the create price seen so far
        self.trough = 1.0          # lowest price over the create price since the peak
        self.buy_sol = 0           # lamports bought by anyone
        self.sell_sol = 0          # lamports sold by anyone
        self.dev_sold = False
        self.last_trade_slot = created
        self.done = False          # never buy it again (exited, filtered out, completed)
        self.entered = False       # the bot already bought this coin once
        self.complete = False

    def price_ratio(self, vq, vt):
        """Current price over the create price (1.0 at creation)."""
        if self.vq0 <= 0 or self.vt0 <= 0 or vq <= 0 or vt <= 0:
            return 1.0
        return (vq * self.vt0) / (vt * self.vq0)


class Position:
    """Bookkeeping for one open pullback position."""

    __slots__ = ("opened", "peak", "tx_paid", "basis", "trimmed",
                 "dev_sold_at_open")

    def __init__(self, opened, basis, dev_sold_at_open):
        self.opened = opened
        self.peak = 0              # best close value seen (lamports)
        self.tx_paid = 0           # tx fees paid on this position's fills (lamports)
        self.basis = basis         # cost of the tokens still held (lamports)
        self.trimmed = False       # the half-bank trim already happened
        self.dev_sold_at_open = dev_sold_at_open


class PullbackRider:
    def __init__(self):
        self.coins = {}           # mint -> Coin, first-seen order
        self.held = {}            # mint -> Position, first-opened order
        self.probes = {}          # mint -> True once its probe sell is out
        self.my_fills = 0         # the bot's own filled orders (harness closes excluded)
        self.my_coins = {}        # mints the bot's own fills touched (insertion order)
        self.first_time = None
        self.last_time = None

    def _sell_value(self, tokens, vq, vt):
        """What selling `tokens` against (vq, vt) pays after the curve fee."""
        if tokens <= 0 or vt <= 0:
            return 0
        quote = tokens * vq // (vt + tokens)
        return quote - (quote * CURVE_FEE_BPS + 9999) // 10000

    def _entry_ready(self, coin, curve):
        """The pullback test: strong early run, then a dip that still holds above water."""
        real_sol = curve.real_quote_reserves / LAMPORTS_PER_SOL
        if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
            return False
        if SKIP_DEV_SOLD and coin.dev_sold:
            return False
        if coin.sell_sol > 0 and coin.buy_sol < MIN_BUY_SELL_RATIO * coin.sell_sol:
            return False
        ratio = coin.price_ratio(curve.virtual_quote_reserves,
                                 curve.virtual_token_reserves)
        if coin.peak < MIN_PEAK_RUNUP:
            return False
        if ratio > coin.peak * (1.0 - MIN_PULLBACK):
            return False
        if ratio < coin.trough * (1.0 + BOUNCE):
            return False
        if ratio < MIN_ABOVE_CREATE:
            return False
        return True

    def on_batch(self, state, batch):
        now = batch["slot"]
        for event in batch["events"]:
            block_time = event.get("block_time")
            if block_time is not None:
                if self.first_time is None:
                    self.first_time = block_time
                if self.last_time is None or block_time > self.last_time:
                    self.last_time = block_time
            kind = event["event"]
            if kind == "CreateEvent":
                self.coins[event["mint"]] = Coin(
                    event["slot"], event["creator"],
                    int(event["virtual_quote_reserves"]),
                    int(event["virtual_token_reserves"]))
            elif kind == "TradeEvent":
                coin = self.coins.get(event["mint"])
                if coin is None:
                    continue
                sol = int(event["sol_amount"])
                coin.last_trade_slot = event["slot"]
                # The event carries its own reserves: measure the tape trade by trade,
                # not against the batch's final curve state.
                ratio = coin.price_ratio(int(event["virtual_quote_reserves"]),
                                         int(event["virtual_token_reserves"]))
                if ratio > coin.peak:
                    coin.peak = ratio
                    coin.trough = ratio
                elif ratio < coin.trough:
                    coin.trough = ratio
                if event["is_buy"]:
                    coin.buy_sol += sol
                else:
                    coin.sell_sol += sol
                    if event["user"] == coin.creator:
                        coin.dev_sold = True
            else:  # CompleteEvent
                coin = self.coins.get(event["mint"])
                if coin is not None:
                    coin.complete = True
                    coin.done = True

        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports()
        orders = []
        pending_by_mint = {}
        for pending in state.pending.values():
            pending_by_mint[pending.order["mint"]] = True

        # --- fills: count the bot's own, track tx fees per position --------
        for fill in batch["fills"]:
            if fill["client_id"].startswith("_"):
                self.held.pop(fill["mint"], None)
                continue
            if fill["status"] != "filled":
                continue
            self.my_fills += 1
            self.my_coins[fill["mint"]] = True
            position = self.held.get(fill["mint"])
            if position is not None:
                position.tx_paid += int(fill["tx_fee_lamports"])
                if fill["side"] == "sell":
                    held_now = state.positions.get(fill["mint"])
                    if held_now is None:
                        self.held.pop(fill["mint"], None)
            elif fill["side"] == "buy":
                coin = self.coins.get(fill["mint"])
                held_now = state.positions.get(fill["mint"])
                basis = int(held_now.cost_lamports) if held_now is not None else 0
                self.held[fill["mint"]] = Position(
                    fill["slot"], basis,
                    coin.dev_sold if coin is not None else False)

        # --- exits first ----------------------------------------------------
        selling = {}
        for mint in list(self.held):
            position = self.held[mint]
            held_now = state.positions.get(mint)
            if held_now is None:
                if mint not in pending_by_mint:
                    self.held.pop(mint)
                    coin = self.coins.get(mint)
                    if coin is not None:
                        coin.done = True
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            tokens = int(held_now.token_amount)
            if tokens <= 0:
                continue
            value = self._sell_value(tokens, curve.virtual_quote_reserves,
                                     curve.virtual_token_reserves) - tx
            basis = position.basis + position.tx_paid
            if value > position.peak:
                position.peak = value
            exit_all = False
            trim_half = False
            if basis > 0:
                if value >= int(basis * (1.0 + TAKE_PROFIT)):
                    exit_all = True
                elif value <= int(basis * (1.0 - STOP_LOSS)):
                    exit_all = True
                elif (position.peak >= int(basis * (1.0 + TRAIL_ARM))
                        and value <= int(position.peak * (1.0 - TRAIL))):
                    exit_all = True
                elif (not position.trimmed
                        and value >= int(basis * (1.0 + TRIM_AT))):
                    trim_half = True
            if not exit_all and not trim_half and now - position.opened >= MAX_HOLD_SLOTS:
                exit_all = True
            coin = self.coins.get(mint)
            if coin is not None:
                if (EXIT_ON_DEV_SELL and coin.dev_sold
                        and not position.dev_sold_at_open):
                    exit_all = True
                    trim_half = False
                if now - coin.last_trade_slot >= STALE_SLOTS:
                    exit_all = True
                    trim_half = False
            if not exit_all and not trim_half:
                continue
            if len(orders) >= cap:
                break
            if trim_half:
                half = tokens // 2
                if half <= 0:
                    continue
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": half,
                               "max_slippage_bps": SELL_SLIPPAGE_BPS})
                position.trimmed = True
                position.basis = position.basis // 2
                position.tx_paid = position.tx_paid // 2
                position.peak = value // 2
                selling[mint] = True
            else:
                orders.append({"side": "sell", "mint": mint,
                               "token_amount": tokens,
                               "max_slippage_bps": SELL_SLIPPAGE_BPS})
                selling[mint] = True
                if coin is not None:
                    coin.done = True

        # --- probe round trips: sell the probe as soon as its buy has landed --
        for mint in list(self.probes):
            held_now = state.positions.get(mint)
            curve = state.curves.get(mint)
            if held_now is None:
                if mint not in pending_by_mint:
                    self.probes.pop(mint)
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": int(held_now.token_amount),
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            self.probes[mint] = True

        # --- entries: one stake per coin that shows strength, then a dip ----
        free = state.free_cash_lamports()
        open_count = state.committed_position_count()
        max_positions = state.hello["caps"]["max_open_positions"]
        for mint, coin in self.coins.items():
            if len(orders) >= cap:
                break
            if coin.done or coin.complete or coin.entered:
                continue
            age = now - coin.created
            if age > MAX_AGE_SLOTS:
                coin.done = True
                continue
            if age < MIN_AGE_SLOTS:
                continue
            if open_count >= max_positions:
                continue
            if mint in pending_by_mint or mint in self.probes:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if not self._entry_ready(coin, curve):
                continue
            # Size at 85% of the size cap: reserves can shrink in the
            # 2-slot latency between deciding and filling, and an order
            # over the cap at fill time is rejected (and still pays tx).
            amount = min(_lamports(STAKE_SOL),
                         state.max_buy_lamports(mint) * 85 // 100)
            if amount < _lamports(MIN_BUY_SOL):
                continue
            if free < amount + tx:
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": amount,
                           "max_slippage_bps": BUY_SLIPPAGE_BPS})
            free -= amount + tx
            coin.entered = True
            open_count += 1

        # --- guardrail pacing: open a probe when fills or coins lag ----------
        if (len(self.probes) < MAX_PROBES
                and self.first_time is not None and self.last_time is not None):
            # Back-loaded pace: launches arrive in bursts, so a linear
            # target over-fires probes early. Squaring the pace keeps the
            # target low while entries accumulate and only calls for
            # probes late if the guardrail is genuinely at risk.
            pace = min(1.0, (self.last_time - self.first_time) / PACE_SECONDS)
            pace = pace * pace
            need_coins = len(self.my_coins) < int(TARGET_COINS * pace + 0.999999)
            need_fills = self.my_fills < int(TARGET_FILLS * pace + 0.999999)
            if need_coins or need_fills:
                min_real = (PROBE_SOL * makefun_bot.BPS
                            / state.hello["caps"]["max_order_bps_of_real_reserve"])
                traded = []
                seen = {}
                for event in batch["events"]:
                    if event["event"] == "TradeEvent" and event["mint"] not in seen:
                        seen[event["mint"]] = True
                        traded.append(event["mint"])
                for mint in traded:
                    if len(orders) >= cap or len(self.probes) >= MAX_PROBES:
                        break
                    if (mint in self.held or mint in self.probes
                            or mint in pending_by_mint
                            or mint in state.positions):
                        continue
                    if need_coins and not need_fills and mint in self.my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves / LAMPORTS_PER_SOL < min_real * 1.5:
                        continue
                    if free < _lamports(PROBE_SOL) + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": _lamports(PROBE_SOL),
                                   "max_slippage_bps": BUY_SLIPPAGE_BPS})
                    self.probes[mint] = False
                    free -= _lamports(PROBE_SOL) + tx
        return orders


if __name__ == "__main__":
    makefun_bot.run(PullbackRider().on_batch)
