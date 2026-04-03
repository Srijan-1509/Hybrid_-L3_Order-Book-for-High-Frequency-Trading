#!/usr/bin/env python3
"""
l3_fpga_driver.py — Drives L3 Order Book from Python on ZCU102 ARM
=====================================================================

This script runs on the ZCU102's ARM Cortex-A53 (under PetaLinux or bare
Linux) and communicates with the PL via memory-mapped AXI registers.

Usage:
    sudo python3 l3_fpga_driver.py              # run L3 test vectors
    sudo python3 l3_fpga_driver.py --gen 100     # 100 random orders

Requires:
    - The FPGA bitstream loaded with the L3 order book design
    - Root access (for /dev/mem)
    - order_encoder.py in the same directory
"""

import mmap
import struct
import os
import sys
import time
import argparse

# Import from the order encoder
from order_encoder import (
    encode_order, decode_order, generate_l3_test_vectors,
    MSG_ADD, MSG_CANCEL, MSG_MODIFY, SIDE_BID, SIDE_ASK,
    MSG_TYPE_NAMES, SIDE_NAMES, BASE_PRICE
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AXI ADDRESS MAP (must match Vivado Address Editor)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BRAM_BASE    = 0xA0000000   # AXI BRAM Controller — order storage
BRAM_SIZE    = 0x4000       # 16 KB (fits 1365 orders × 12 bytes)

CTRL_BASE    = 0xA0010000   # GPIO Control — start, order_count, base_price
CTRL_SIZE    = 0x10000      # 64 KB

STATUS_BASE  = 0xA0020000   # GPIO Status  — best_bid_price, best_ask_price
STATUS_SIZE  = 0x10000

STATUS2_BASE = 0xA0030000   # GPIO Status2 — best_bid_qty, best_ask_qty
STATUS2_SIZE = 0x10000

# AXI GPIO register offsets
GPIO_DATA_CH1 = 0x0000      # Channel 1 data register
GPIO_DATA_CH2 = 0x0008      # Channel 2 data register


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FPGA DRIVER CLASS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FPGADriver:
    """Low-level driver for L3 Order Book FPGA design on ZCU102."""

    def __init__(self):
        """Open /dev/mem and mmap the AXI peripherals."""
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.bram    = mmap.mmap(self.fd, BRAM_SIZE,    offset=BRAM_BASE)
        self.ctrl    = mmap.mmap(self.fd, CTRL_SIZE,    offset=CTRL_BASE)
        self.status  = mmap.mmap(self.fd, STATUS_SIZE,  offset=STATUS_BASE)
        self.status2 = mmap.mmap(self.fd, STATUS2_SIZE, offset=STATUS2_BASE)

    def write32(self, mm, offset, value):
        """Write a 32-bit word to a memory-mapped region."""
        mm.seek(offset)
        mm.write(struct.pack('<I', value & 0xFFFFFFFF))

    def read32(self, mm, offset):
        """Read a 32-bit word from a memory-mapped region."""
        mm.seek(offset)
        return struct.unpack('<I', mm.read(4))[0]

    # ─────────────────────────────────────────────────────────────────────────
    # BRAM ACCESS
    # ─────────────────────────────────────────────────────────────────────────

    def load_orders(self, orders):
        """
        Encode and write orders into BRAM.

        Each 96-bit order is stored as 3 consecutive 32-bit words:
          Word 0 (offset +0): msg_data[31:0]
          Word 1 (offset +4): msg_data[63:32]
          Word 2 (offset +8): msg_data[95:64]
        """
        max_orders = BRAM_SIZE // 12  # 1365 for 16 KB
        if len(orders) > max_orders:
            raise ValueError(
                f"Too many orders: {len(orders)} > {max_orders} (BRAM limit)")

        for i, o in enumerate(orders):
            # Encode order to 12 bytes
            if o["msg_type"] not in (MSG_ADD, MSG_CANCEL, MSG_MODIFY):
                # Invalid message type — encode raw (for error testing)
                byte0 = (o["msg_type"] & 0x07) | ((o["side"] & 0x01) << 3)
                raw = struct.pack('<BBHHHI',
                    byte0, o["symbol_id"], o["order_id"],
                    o["price_idx"] & 0x0FFF, o["seq_num"], o["quantity"])
            else:
                raw = encode_order(
                    o["msg_type"], o["side"], o["order_id"],
                    o["price_idx"], o["quantity"],
                    o["symbol_id"], o["seq_num"])

            # Unpack into 3 × 32-bit words
            w0, w1, w2 = struct.unpack('<III', raw)

            # Write to BRAM
            base_offset = i * 12
            self.write32(self.bram, base_offset + 0, w0)
            self.write32(self.bram, base_offset + 4, w1)
            self.write32(self.bram, base_offset + 8, w2)

        return len(orders)

    # ─────────────────────────────────────────────────────────────────────────
    # CONTROL
    # ─────────────────────────────────────────────────────────────────────────

    def set_base_price(self, price):
        """Set the base_price configuration register (GPIO Channel 2)."""
        self.write32(self.ctrl, GPIO_DATA_CH2, price)

    def start_processing(self, count):
        """
        Trigger the PL to process 'count' orders from BRAM.

        ctrl_word format: [16:1] = order_count, [0] = start pulse
        The start bit is edge-detected in RTL, so we write 1 then 0.
        """
        ctrl_val = ((count & 0xFFFF) << 1) | 0x1
        self.write32(self.ctrl, GPIO_DATA_CH1, ctrl_val)
        # Small delay to ensure the PL sees the rising edge
        time.sleep(0.001)
        # Clear start bit
        self.write32(self.ctrl, GPIO_DATA_CH1, 0)

    def wait_done(self, timeout_s=1.0):
        """
        Wait for the PL to finish processing.

        In a production design, we'd read a 'done' status register.
        For now, we use a conservative time-based wait:
          28 cycles/order × 4ns/cycle = 112 ns/order
          For 100 orders: ~11.2 µs (negligible)
        """
        # Conservative wait: 1ms per 100 orders + 10ms overhead
        # The actual PL processing is far faster than this
        time.sleep(timeout_s)

    # ─────────────────────────────────────────────────────────────────────────
    # BBO READBACK
    # ─────────────────────────────────────────────────────────────────────────

    def read_bbo(self):
        """
        Read the Best Bid/Offer (BBO) from status registers.

        Returns:
            tuple: (bid_price, ask_price, bid_qty, ask_qty)
        """
        bid_price = self.read32(self.status,  GPIO_DATA_CH1)
        ask_price = self.read32(self.status,  GPIO_DATA_CH2)
        bid_qty   = self.read32(self.status2, GPIO_DATA_CH1)
        ask_qty   = self.read32(self.status2, GPIO_DATA_CH2)
        return bid_price, ask_price, bid_qty, ask_qty

    # ─────────────────────────────────────────────────────────────────────────
    # CLEANUP
    # ─────────────────────────────────────────────────────────────────────────

    def close(self):
        """Unmap memory regions and close /dev/mem."""
        self.bram.close()
        self.ctrl.close()
        self.status.close()
        self.status2.close()
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_test_vectors():
    """Run the standard L3 test vectors and display BBO."""
    orders = generate_l3_test_vectors()

    print(f"  Generated {len(orders)} test vectors")
    print(f"  Base price: {BASE_PRICE}")
    print()

    # Show first 10 orders
    print("  ── First 10 orders ──")
    for i, o in enumerate(orders[:10]):
        mt = MSG_TYPE_NAMES.get(o['msg_type'], f"?{o['msg_type']}")
        sd = SIDE_NAMES.get(o['side'], '?')
        print(f"  [{i:3d}] {mt:<6s} {sd:<3s}  "
              f"OID={o['order_id']:<5d}  "
              f"price={BASE_PRICE + o['price_idx']:<6d}  "
              f"qty={o['quantity']:<8d}")
    print()

    with FPGADriver() as drv:
        # Configure
        drv.set_base_price(BASE_PRICE)

        # Load orders to BRAM
        n = drv.load_orders(orders)
        print(f"  Loaded {n} orders into BRAM")

        # Trigger processing
        print(f"  Starting PL processing...")
        t0 = time.perf_counter_ns()
        drv.start_processing(n)

        # Wait for completion
        drv.wait_done(timeout_s=0.1)
        t1 = time.perf_counter_ns()

        # Read BBO
        bp, ap, bq, aq = drv.read_bbo()

        print()
        print("  ╔══════════════════════════════════╗")
        print("  ║     BBO — Best Bid / Offer       ║")
        print("  ╠══════════════════════════════════╣")
        print(f"  ║  Best Bid: {bq:>8d} @ {bp:>6d}     ║")
        print(f"  ║  Best Ask: {aq:>8d} @ {ap:>6d}     ║")
        print("  ╠══════════════════════════════════╣")
        print(f"  ║  Wall time: {(t1-t0)/1e6:>8.3f} ms        ║")
        print("  ╚══════════════════════════════════╝")


def main():
    parser = argparse.ArgumentParser(
        description="L3 Order Book FPGA Driver for ZCU102")
    parser.add_argument("--gen", type=int, default=0,
                        help="Generate N random orders instead of test vectors")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  L3 Order Book — ZCU102 FPGA Driver")
    print("  AMD Zynq UltraScale+ MPSoC")
    print("=" * 60)
    print()

    if os.geteuid() != 0:
        print("  ERROR: This script requires root access for /dev/mem")
        print("  Usage: sudo python3 l3_fpga_driver.py")
        sys.exit(1)

    if args.gen > 0:
        import random
        print(f"  Generating {args.gen} random orders...")
        orders = []
        for i in range(args.gen):
            orders.append({
                "msg_type":  random.choice([MSG_ADD, MSG_CANCEL, MSG_MODIFY]),
                "side":      random.choice([SIDE_BID, SIDE_ASK]),
                "order_id":  random.randint(0, 1000),
                "price_idx": random.randint(0, 200),
                "quantity":  random.randint(1, 10000),
                "symbol_id": 0,
                "seq_num":   i + 1,
                "label":     f"RANDOM order {i}",
            })

        with FPGADriver() as drv:
            drv.set_base_price(BASE_PRICE)
            n = drv.load_orders(orders)
            print(f"  Loaded {n} random orders")
            drv.start_processing(n)
            drv.wait_done(0.1)
            bp, ap, bq, aq = drv.read_bbo()
            print(f"\n  BBO: BID {bq}@{bp}  ASK {aq}@{ap}")
    else:
        run_test_vectors()

    print()


if __name__ == "__main__":
    main()
