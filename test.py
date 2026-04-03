#!/usr/bin/env python3
"""
test_orders.py  –  PYNQ-Z2 order-book test driver
====================================================
Drives bitmap_axi_wrapper via AXI4-Lite using the PYNQ MMIO class.
Replicates the same six test cases from tb.v so results are directly
comparable between simulation and hardware.

HOW TO RUN
----------
1. Copy this file and your bitstream (.bit) + hardware handoff (.hwh)
   to the PYNQ-Z2 SD card (or transfer via scp).
2. Open Jupyter on the board (http://<board-ip>:9090, password: xilinx).
3. Either run this script directly:
       !python3 test_orders.py
   or paste cells into a notebook.

REQUIREMENTS
------------
* PYNQ image >= 2.7  (pynq package pre-installed)
* Bitstream generated from bitmap_axi_wrapper connected at AXI_BASE_ADDR.
  Default address 0x43C0_0000  (HP0 AXI GP0, Vivado Block Design default).
  Change AXI_BASE_ADDR below if your address assignment differs.

REGISTER MAP (matches bitmap_axi_wrapper.v)
-------------------------------------------
  0x00  CTRL        [0]=update_valid [1]=side [2]=sw_reset [3]=use_vio
  0x04  UPDATE_IDX  [11:0]
  0x08  UPDATE_QTY  [31:0]
  0x0C  BASE_PRICE  [31:0]
  0x10  STATUS  RO  [0]=bbo_valid
  0x14  BID_PRICE RO
  0x18  ASK_PRICE RO
  0x1C  BID_QTY   RO
  0x20  ASK_QTY   RO
"""

import time
from pynq import MMIO, Overlay

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION – edit these to match your design
# ─────────────────────────────────────────────────────────────────────────────
BITSTREAM_PATH  = "bitmap_top.bit"      # path to your .bit file
AXI_BASE_ADDR   = 0x43C0_0000           # AXI4-Lite base address from Vivado
AXI_RANGE       = 0x0000_1000           # 4 KB window is more than enough
BASE_PRICE      = 10000                 # must match BASE_PRICE param in RTL
LOAD_OVERLAY    = True                  # set False to skip Overlay() if already programmed

# ─────────────────────────────────────────────────────────────────────────────
# Register offsets (byte addresses relative to AXI_BASE_ADDR)
# ─────────────────────────────────────────────────────────────────────────────
CTRL_OFF      = 0x00
IDX_OFF       = 0x04
QTY_OFF       = 0x08
BASEPRICE_OFF = 0x0C
STATUS_OFF    = 0x10
BID_PRICE_OFF = 0x14
ASK_PRICE_OFF = 0x18
BID_QTY_OFF   = 0x1C
ASK_QTY_OFF   = 0x20

# CTRL bit masks
BIT_UPDATE_VALID = 0x01
BIT_SIDE_ASK     = 0x02   # 0=bid, 1=ask
BIT_SW_RESET     = 0x04
BIT_USE_VIO      = 0x08

# ─────────────────────────────────────────────────────────────────────────────
# ORDER DATA – all six test-bench test cases
#
# Each order is a dict:
#   idx   : price index   (price = BASE_PRICE + idx)
#   qty   : quantity      (0 = cancel)
#   side  : "bid" or "ask"
#   label : human-readable annotation
# ─────────────────────────────────────────────────────────────────────────────

# Test 1 – BASIC: single bid + single ask
TEST1_ORDERS = [
    {"idx": 5,  "qty": 500, "side": "bid", "label": "BASIC bid  @10005 qty=500"},
    {"idx": 10, "qty": 200, "side": "ask", "label": "BASIC ask  @10010 qty=200"},
]
TEST1_EXPECT = {"bid_price": BASE_PRICE+5,  "ask_price": BASE_PRICE+10,
                "bid_qty":   500,           "ask_qty":   200}

# Test 2 – CANCEL: two bids, cancel the better one
TEST2_ORDERS = [
    {"idx": 3,  "qty": 100, "side": "bid", "label": "CANCEL seed bid @10003 qty=100"},
    {"idx": 5,  "qty": 500, "side": "bid", "label": "CANCEL seed bid @10005 qty=500"},
    {"idx": 10, "qty": 200, "side": "ask", "label": "CANCEL seed ask @10010 qty=200"},
    {"idx": 5,  "qty": 0,   "side": "bid", "label": "CANCEL cancel   @10005 qty=0"},  # <-- triggers
]
TEST2_EXPECT = {"bid_price": BASE_PRICE+3,  "ask_price": BASE_PRICE+10,
                "bid_qty":   100,           "ask_qty":   200}

# Test 3 – BYPASS: two consecutive updates in same L0 block (idx 0 and idx 1)
TEST3_SEED   = [{"idx": 60, "qty": 300, "side": "ask", "label": "BYPASS seed ask @10060 qty=300"}]
TEST3_BURST  = [
    {"idx": 0, "qty": 100, "side": "bid", "label": "BYPASS bid @10000 qty=100"},
    {"idx": 1, "qty": 999, "side": "bid", "label": "BYPASS bid @10001 qty=999"},  # best
]
TEST3_EXPECT = {"bid_price": BASE_PRICE+1,  "ask_price": BASE_PRICE+60,
                "bid_qty":   999,           "ask_qty":   300}

# Test 4 – BURST: 8 alternating bid/ask updates spread across the book
#   Even indices → bid at idx i*10, odd indices → ask at idx i*10
TEST4_ORDERS = [
    {"idx": i*10, "qty": (i+1)*100,
     "side": "ask" if i % 2 else "bid",
     "label": f"BURST {'ask' if i%2 else 'bid'} @{BASE_PRICE+i*10} qty={(i+1)*100}"}
    for i in range(8)
]
# Bids: idx 0(100), 20(300), 40(500), 60(700)  → best bid idx 60 qty 700
# Asks: idx 10(200), 30(400), 50(600), 70(800) → best ask idx 10 qty 200
TEST4_EXPECT = {"bid_price": BASE_PRICE+60, "ask_price": BASE_PRICE+10,
                "bid_qty":   700,           "ask_qty":   200}

# Test 5 – QTY_CHANGE: update qty at same price level
TEST5_SEED   = [
    {"idx": 5,  "qty": 500, "side": "bid", "label": "QTYCHANGE seed bid @10005 qty=500"},
    {"idx": 10, "qty": 200, "side": "ask", "label": "QTYCHANGE seed ask @10010 qty=200"},
]
TEST5_UPDATE = [{"idx": 5, "qty": 123, "side": "bid", "label": "QTYCHANGE update bid @10005 qty=123"}]
TEST5_EXPECT = {"bid_price": BASE_PRICE+5,  "ask_price": BASE_PRICE+10,
                "bid_qty":   123,           "ask_qty":   200}

# Test 6 – LATENCY: 20 cancel+add pairs, alternating best bid between idx 10 and idx 20
#   Generated programmatically below.

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def setup_mmio():
    """Program the FPGA (if needed) and return an MMIO handle."""
    if LOAD_OVERLAY:
        print(f"  Loading overlay: {BITSTREAM_PATH}")
        ol = Overlay(BITSTREAM_PATH)
        print("  Overlay loaded.")
    mmio = MMIO(AXI_BASE_ADDR, AXI_RANGE)
    return mmio


def hw_reset(mmio):
    """Assert sw_reset for a few microseconds then release."""
    mmio.write(CTRL_OFF, BIT_SW_RESET)
    time.sleep(0.001)   # 1 ms >> any pipeline drain
    mmio.write(CTRL_OFF, 0x00)
    time.sleep(0.001)


def send_order(mmio, idx, qty, side, inter_order_delay=0.0001):
    """Write a single order to the AXI register interface.

    Steps:
      1. Write UPDATE_IDX
      2. Write UPDATE_QTY
      3. Assert update_valid (and side) in CTRL
      4. Wait inter_order_delay seconds (default 100 µs >> 9-cycle latency)
      5. De-assert update_valid
    """
    side_bit = BIT_SIDE_ASK if side == "ask" else 0x00
    mmio.write(IDX_OFF,  idx)
    mmio.write(QTY_OFF,  qty)
    mmio.write(CTRL_OFF, BIT_UPDATE_VALID | side_bit)
    time.sleep(inter_order_delay)
    mmio.write(CTRL_OFF, 0x00)


def read_bbo(mmio):
    """Read and return current BBO as a dict."""
    return {
        "bbo_valid":  mmio.read(STATUS_OFF)    & 0x01,
        "bid_price":  mmio.read(BID_PRICE_OFF),
        "ask_price":  mmio.read(ASK_PRICE_OFF),
        "bid_qty":    mmio.read(BID_QTY_OFF),
        "ask_qty":    mmio.read(ASK_QTY_OFF),
    }


def poll_bbo_valid(mmio, timeout=0.01):
    """Poll STATUS[0] until bbo_valid goes high or timeout (seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if mmio.read(STATUS_OFF) & 0x01:
            return True
    return False


def check(test_name, mmio, expect):
    """Poll for bbo_valid then compare BBO against expected values."""
    if not poll_bbo_valid(mmio):
        print(f"  FAIL [{test_name}]  bbo_valid never asserted within timeout")
        return False

    bbo = read_bbo(mmio)
    ok  = (bbo["bid_price"] == expect["bid_price"] and
           bbo["ask_price"] == expect["ask_price"] and
           bbo["bid_qty"]   == expect["bid_qty"]   and
           bbo["ask_qty"]   == expect["ask_qty"])

    if ok:
        print(f"  PASS [{test_name}]  "
              f"BID {bbo['bid_qty']}@{bbo['bid_price']}  "
              f"ASK {bbo['ask_qty']}@{bbo['ask_price']}")
    else:
        print(f"  FAIL [{test_name}]")
        print(f"    GOT   BID {bbo['bid_qty']}@{bbo['bid_price']}  "
              f"ASK {bbo['ask_qty']}@{bbo['ask_price']}")
        print(f"    WANT  BID {expect['bid_qty']}@{expect['bid_price']}  "
              f"ASK {expect['ask_qty']}@{expect['ask_price']}")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_basic(mmio):
    print("\n─── TEST 1: BASIC ───────────────────────────────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    for o in TEST1_ORDERS:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])
        time.sleep(0.001)   # let this order settle before the next

    return check("BASIC", mmio, TEST1_EXPECT)


def test_cancel(mmio):
    print("\n─── TEST 2: CANCEL ──────────────────────────────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    for o in TEST2_ORDERS:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])
        time.sleep(0.001)

    return check("CANCEL", mmio, TEST2_EXPECT)


def test_bypass(mmio):
    print("\n─── TEST 3: BYPASS (same-block consecutive) ─────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    # Seed the ask
    for o in TEST3_SEED:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])
        time.sleep(0.005)   # let seed settle

    # Fire the two back-to-back bids with minimal gap
    for o in TEST3_BURST:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"], inter_order_delay=0.00002)

    return check("BYPASS", mmio, TEST3_EXPECT)


def test_burst(mmio):
    print("\n─── TEST 4: BURST (8 orders, ~100 µs apart) ─────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    for o in TEST4_ORDERS:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])
        time.sleep(0.0002)   # 200 µs >> pipeline

    return check("BURST", mmio, TEST4_EXPECT)


def test_qty_change(mmio):
    print("\n─── TEST 5: QTY_CHANGE ──────────────────────────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    for o in TEST5_SEED:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])
        time.sleep(0.001)

    time.sleep(0.005)   # settle

    for o in TEST5_UPDATE:
        print(f"    → {o['label']}")
        send_order(mmio, o["idx"], o["qty"], o["side"])

    return check("QTY_CHANGE", mmio, TEST5_EXPECT)


def test_latency(mmio, n_samples=20):
    """Cancel + add pattern guarantees a BBO price change every sample.
    Measures round-trip time from Python's perspective (includes AXI
    overhead, so absolute values will be >> 9 FPGA cycles).
    The *change* in measured times across samples is meaningful.
    """
    print("\n─── TEST 6: LATENCY MEASUREMENT ─────────────────────────────")
    hw_reset(mmio)
    mmio.write(BASEPRICE_OFF, BASE_PRICE)

    # Seed: standing ask at idx 50, initial bid at idx 10
    send_order(mmio, 50, 999, "ask"); time.sleep(0.002)
    send_order(mmio, 10, 500, "bid"); time.sleep(0.005)

    latencies = []
    for s in range(n_samples):
        cur_idx = 20 if (s % 2) else 10  # current best
        new_idx = 10 if (s % 2) else 20  # new best

        # Cancel current best
        send_order(mmio, cur_idx, 0, "bid")
        time.sleep(0.001)   # let cancel RMW commit

        # Add new best, time the round-trip
        t0 = time.perf_counter()
        send_order(mmio, new_idx, s + 1, "bid")
        valid = poll_bbo_valid(mmio, timeout=0.05)
        t1 = time.perf_counter()

        dt_us = (t1 - t0) * 1e6
        if valid:
            latencies.append(dt_us)
            print(f"  sample {s:02d} : {dt_us:7.1f} µs  "
                  f"(BID {s+1}@{BASE_PRICE+new_idx})")
        else:
            print(f"  sample {s:02d} : TIMEOUT")

        time.sleep(0.002)   # settle before next pair

    if latencies:
        print("  ─────────────────────────────────")
        print(f"  min : {min(latencies):.1f} µs")
        print(f"  max : {max(latencies):.1f} µs")
        print(f"  avg : {sum(latencies)/len(latencies):.1f} µs")
        print("  Note: includes AXI + Python overhead. FPGA pipeline = 9 cycles (36 ns).")
    return len(latencies) == n_samples


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  bitmap.v Order Book  –  PYNQ-Z2 Hardware Test")
    print(f"  AXI base addr : 0x{AXI_BASE_ADDR:08X}")
    print(f"  BASE_PRICE    : {BASE_PRICE}")
    print("=" * 65)

    mmio = setup_mmio()

    results = {}
    results["BASIC"]      = test_basic(mmio)
    results["CANCEL"]     = test_cancel(mmio)
    results["BYPASS"]     = test_bypass(mmio)
    results["BURST"]      = test_burst(mmio)
    results["QTY_CHANGE"] = test_qty_change(mmio)
    results["LATENCY"]    = test_latency(mmio)

    passed = sum(1 for v in results.values() if v)
    total  = len(results)

    print("\n" + "=" * 65)
    print("  SUMMARY")
    for name, ok in results.items():
        status = "PASS ✓" if ok else "FAIL ✗"
        print(f"    {name:<12} {status}")
    print(f"\n  Tests passed : {passed} / {total}")
    print("=" * 65)


if __name__ == "__main__":
    main()