#!/usr/bin/env python3
"""
order_encoder.py — NYSE Pillar-Inspired Binary Order Encoder
=============================================================

Fixed-width 96-bit (12-byte) binary message format designed for
zero-overhead FPGA parsing.  Modeled after the NYSE Pillar Stream
Protocol: all fields are fixed-length, little-endian, with no
tag-value parsing required.

Message Layout (96 bits, little-endian byte order):
─────────────────────────────────────────────────────
  Byte 0:   [2:0] msg_type   001=ADD, 010=CANCEL, 011=MODIFY
            [3]   side        0=BID, 1=ASK
            [7:4] reserved    (zero)
  Byte 1:   [7:0] symbol_id  Instrument ID (0–255)
  Byte 2-3: [15:0] order_id  Unique order identifier (little-endian u16)
  Byte 4-5: [11:0] price_idx Price level index 0–4095
            [15:12] reserved  (zero)
  Byte 6-7: [15:0] seq_num   Sequence number (little-endian u16)
  Byte 8-11:[31:0] quantity   Order quantity (little-endian u32)

FIX Protocol Tag Mapping (for reference):
  Tag 35  (MsgType)   → msg_type
  Tag 54  (Side)      → side
  Tag 11  (ClOrdID)   → order_id
  Tag 55  (Symbol)    → symbol_id
  Tag 44  (Price)     → price_idx (relative to base_price)
  Tag 38  (OrderQty)  → quantity

Usage:
  python order_encoder.py                  # prints demo + writes test_vectors.mem
  python order_encoder.py --gen N          # generate N random orders
  python order_encoder.py --memfile FILE   # write Verilog $readmemh file
"""

import struct
import random
import argparse
import os

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSTANTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Message types
MSG_ADD    = 0x01   # 001
MSG_CANCEL = 0x02   # 010
MSG_MODIFY = 0x03   # 011

MSG_TYPE_NAMES = {MSG_ADD: "ADD", MSG_CANCEL: "CANCEL", MSG_MODIFY: "MODIFY"}

# Sides
SIDE_BID = 0
SIDE_ASK = 1

SIDE_NAMES = {SIDE_BID: "BID", SIDE_ASK: "ASK"}

# Field widths
ORDER_ID_WIDTH  = 16   # bits
PRICE_IDX_WIDTH = 12   # bits
QTY_WIDTH       = 32   # bits
SYMBOL_ID_WIDTH = 8    # bits
SEQ_NUM_WIDTH   = 16   # bits

# Max values
MAX_ORDER_ID  = (1 << ORDER_ID_WIDTH)  - 1   # 65535
MAX_PRICE_IDX = (1 << PRICE_IDX_WIDTH) - 1   # 4095
MAX_QTY       = (1 << QTY_WIDTH)       - 1   # 4294967295
MAX_SYMBOL_ID = (1 << SYMBOL_ID_WIDTH) - 1   # 255
MAX_SEQ_NUM   = (1 << SEQ_NUM_WIDTH)   - 1   # 65535

# FIX tag mapping (for documentation / cross-reference)
FIX_TAG_MAP = {
    35: "msg_type",
    54: "side",
    11: "order_id",
    55: "symbol_id",
    44: "price_idx",
    38: "quantity",
}

# Base price (must match RTL parameter)
BASE_PRICE = 10000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENCODER / DECODER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def encode_order(msg_type, side, order_id, price_idx, quantity,
                 symbol_id=0, seq_num=0):
    """Encode an order into a 12-byte (96-bit) binary message.

    Args:
        msg_type:   MSG_ADD (1), MSG_CANCEL (2), or MSG_MODIFY (3)
        side:       SIDE_BID (0) or SIDE_ASK (1)
        order_id:   16-bit unique order identifier
        price_idx:  12-bit price level index (price = base_price + price_idx)
        quantity:   32-bit order quantity
        symbol_id:  8-bit instrument ID (default 0)
        seq_num:    16-bit sequence number (default 0)

    Returns:
        bytes: 12-byte encoded message (little-endian)
    """
    # Validation
    assert msg_type in (MSG_ADD, MSG_CANCEL, MSG_MODIFY), \
        f"Invalid msg_type: {msg_type}"
    assert side in (SIDE_BID, SIDE_ASK), \
        f"Invalid side: {side}"
    assert 0 <= order_id  <= MAX_ORDER_ID,  f"order_id out of range: {order_id}"
    assert 0 <= price_idx <= MAX_PRICE_IDX, f"price_idx out of range: {price_idx}"
    assert 0 <= quantity  <= MAX_QTY,       f"quantity out of range: {quantity}"
    assert 0 <= symbol_id <= MAX_SYMBOL_ID, f"symbol_id out of range: {symbol_id}"
    assert 0 <= seq_num   <= MAX_SEQ_NUM,   f"seq_num out of range: {seq_num}"

    # Byte 0: msg_type[2:0] | side[3] | reserved[7:4]
    byte0 = (msg_type & 0x07) | ((side & 0x01) << 3)

    # Byte 1: symbol_id
    byte1 = symbol_id & 0xFF

    # Bytes 2-3: order_id (u16 LE)
    # Bytes 4-5: price_idx[11:0] | reserved[15:12]  (u16 LE)
    price_field = price_idx & 0x0FFF

    # Bytes 6-7: seq_num (u16 LE)
    # Bytes 8-11: quantity (u32 LE)

    # Pack: byte0, byte1, order_id(u16), price_field(u16), seq_num(u16), qty(u32)
    raw = struct.pack('<BBHHHI', byte0, byte1, order_id, price_field,
                      seq_num, quantity)
    assert len(raw) == 12
    return raw


def decode_order(raw):
    """Decode a 12-byte binary message into a dict of fields.

    Args:
        raw: bytes of length 12

    Returns:
        dict with keys: msg_type, side, symbol_id, order_id,
                        price_idx, seq_num, quantity, absolute_price
    """
    assert len(raw) == 12, f"Expected 12 bytes, got {len(raw)}"

    byte0, byte1, order_id, price_field, seq_num, quantity = \
        struct.unpack('<BBHHHI', raw)

    msg_type  = byte0 & 0x07
    side      = (byte0 >> 3) & 0x01
    symbol_id = byte1
    price_idx = price_field & 0x0FFF

    return {
        "msg_type":       msg_type,
        "msg_type_name":  MSG_TYPE_NAMES.get(msg_type, f"UNKNOWN({msg_type})"),
        "side":           side,
        "side_name":      SIDE_NAMES.get(side, f"UNKNOWN({side})"),
        "symbol_id":      symbol_id,
        "order_id":       order_id,
        "price_idx":      price_idx,
        "seq_num":        seq_num,
        "quantity":       quantity,
        "absolute_price": BASE_PRICE + price_idx,
    }


def to_hex96(raw):
    """Convert 12-byte message to a 24-char hex string (for Verilog $readmemh).

    The hex string represents the 96-bit value in big-endian hex digit order
    (MSB first) so that Verilog's $readmemh reads it correctly into a
    [95:0] register.
    """
    # raw is little-endian bytes; reverse to get big-endian for hex display
    return raw[::-1].hex()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEST VECTOR GENERATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_l3_test_vectors():
    """Generate a carefully crafted sequence of orders for L3 testbench.

    Returns a list of dicts, each with:
        msg_type, side, order_id, price_idx, quantity, symbol_id, seq_num, label
    """
    orders = []
    seq = 0

    def add(msg_type, side, oid, pidx, qty, label):
        nonlocal seq
        seq += 1
        orders.append({
            "msg_type":  msg_type,
            "side":      side,
            "order_id":  oid,
            "price_idx": pidx,
            "quantity":  qty,
            "symbol_id": 0,
            "seq_num":   seq,
            "label":     label,
        })

    # ── TEST 1: Basic ADD ─────────────────────────────────────────────────
    # Add two bids and one ask.  BBO should be best_bid=idx5, best_ask=idx10
    add(MSG_ADD, SIDE_BID, 1, 5,  500, "T1: ADD bid OID=1 @10005 qty=500")
    add(MSG_ADD, SIDE_BID, 2, 3,  300, "T1: ADD bid OID=2 @10003 qty=300")
    add(MSG_ADD, SIDE_ASK, 3, 10, 200, "T1: ADD ask OID=3 @10010 qty=200")

    # ── TEST 2: Multiple orders at same price ──────────────────────────
    # Add second bid at idx=5.  Aggregate qty at idx5 should be 500+400=900
    add(MSG_ADD, SIDE_BID, 4, 5,  400, "T2: ADD bid OID=4 @10005 qty=400 (same price)")

    # ── TEST 3: Cancel by Order ID ─────────────────────────────────────
    # Cancel OID=1 (qty=500 at idx=5).  Aggregate at idx5 should drop to 400.
    add(MSG_CANCEL, SIDE_BID, 1, 5, 0, "T3: CANCEL bid OID=1 @10005")

    # ── TEST 4: Cancel last order at a price level ─────────────────────
    # Cancel OID=4 (qty=400 at idx=5).  idx5 aggregate → 0, bitmap bit clears.
    # BBO moves to next bid at idx=3 (OID=2, qty=300).
    add(MSG_CANCEL, SIDE_BID, 4, 5, 0, "T4: CANCEL bid OID=4 @10005 (last at level)")

    # ── TEST 5: Modify order quantity ──────────────────────────────────
    # Modify OID=2 (at idx=3) from qty=300 to qty=750.
    add(MSG_MODIFY, SIDE_BID, 2, 3, 750, "T5: MODIFY bid OID=2 @10003 qty 300->750")

    # ── TEST 6: Add more ask orders ───────────────────────────────────
    add(MSG_ADD, SIDE_ASK, 5, 15, 600, "T6: ADD ask OID=5 @10015 qty=600")
    add(MSG_ADD, SIDE_ASK, 6, 10, 350, "T6: ADD ask OID=6 @10010 qty=350 (same price)")
    # Ask at idx10 aggregate = 200 + 350 = 550

    # ── TEST 7: Cancel one ask, verify aggregate ─────────────────────
    add(MSG_CANCEL, SIDE_ASK, 3, 10, 0, "T7: CANCEL ask OID=3 @10010")
    # Ask at idx10 aggregate = 550 - 200 = 350

    # ── TEST 8: Burst of adds across the book ────────────────────────
    for i in range(8):
        oid = 100 + i
        pidx = (i + 1) * 50   # 50,100,150,...,400
        qty = (i + 1) * 100   # 100,200,...,800
        s = SIDE_ASK if (i % 2) else SIDE_BID
        add(MSG_ADD, s, oid, pidx, qty,
            f"T8: BURST {'ask' if s else 'bid'} OID={oid} @{BASE_PRICE+pidx} qty={qty}")

    # ── TEST 9: Invalid message type (msg_type=7) ─────────────────────
    # Feed parser should flag parse_error.  We'll encode it raw.
    orders.append({
        "msg_type":  7,   # invalid
        "side":      0,
        "order_id":  999,
        "price_idx": 0,
        "quantity":  0,
        "symbol_id": 0,
        "seq_num":   seq + 1,
        "label":     "T9: INVALID msg_type=7 (should trigger parse_error)",
    })
    seq += 1

    # ── TEST 10: Rapid cancel+add for latency measurement ─────────────
    # Alternate best bid between idx 20 and idx 30
    add(MSG_ADD, SIDE_BID, 200, 20, 1000, "T10: Seed bid OID=200 @10020 qty=1000")
    add(MSG_ADD, SIDE_ASK, 201, 50,  999, "T10: Seed ask OID=201 @10050 qty=999")

    for s in range(10):
        cur_oid = 200 + (s % 2) * 10       # 200 or 210
        new_oid = 200 + ((s+1) % 2) * 10   # 210 or 200
        cur_pidx = 20 if (s % 2 == 0) else 30
        new_pidx = 30 if (s % 2 == 0) else 20
        add(MSG_CANCEL, SIDE_BID, cur_oid, cur_pidx, 0,
            f"T10: LAT sample {s} cancel OID={cur_oid}")
        add(MSG_ADD, SIDE_BID, new_oid, new_pidx, s + 1,
            f"T10: LAT sample {s} add OID={new_oid} @{BASE_PRICE+new_pidx} qty={s+1}")

    return orders


def write_memfile(orders, filename):
    """Write orders as a Verilog-compatible $readmemh file.

    Each line is 24 hex characters representing one 96-bit message.
    Lines starting with // are comments.
    """
    with open(filename, 'w', encoding='ascii') as f:
        f.write("// ============================================================\n")
        f.write("// L3 Order Book Test Vectors\n")
        f.write("// Generated by order_encoder.py\n")
        f.write(f"// {len(orders)} messages, 96 bits each\n")
        f.write("// ============================================================\n\n")

        for i, o in enumerate(orders):
            # For invalid msg_type, encode raw (bypass validation)
            if o["msg_type"] not in (MSG_ADD, MSG_CANCEL, MSG_MODIFY):
                byte0 = (o["msg_type"] & 0x07) | ((o["side"] & 0x01) << 3)
                raw = struct.pack('<BBHHHI', byte0, o["symbol_id"],
                                  o["order_id"], o["price_idx"] & 0x0FFF,
                                  o["seq_num"], o["quantity"])
            else:
                raw = encode_order(
                    o["msg_type"], o["side"], o["order_id"],
                    o["price_idx"], o["quantity"],
                    o["symbol_id"], o["seq_num"]
                )

            hex_str = to_hex96(raw)
            f.write(f"// [{i:3d}] {o['label']}\n")
            f.write(f"{hex_str}\n\n")

    print(f"  Wrote {len(orders)} test vectors to {filename}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRETTY PRINTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_order(order_dict, raw=None):
    """Pretty-print a decoded order."""
    d = order_dict
    print(f"  {d['msg_type_name']:<6s}  {d['side_name']:<3s}  "
          f"OID={d['order_id']:<5d}  price={d['absolute_price']:<6d}  "
          f"qty={d['quantity']:<8d}  sym={d['symbol_id']}  seq={d['seq_num']}"
          + (f"  hex={raw.hex()}" if raw else ""))


def demo():
    """Demonstrate encoding, decoding, and test vector generation."""
    print("=" * 70)
    print("  NYSE Pillar-Inspired Binary Order Encoder")
    print("  96-bit fixed-width messages for FPGA L3 Order Book")
    print("=" * 70)

    # ── Single encode/decode demo ──
    print("\n-- Encode/Decode Demo --")
    raw = encode_order(MSG_ADD, SIDE_BID, order_id=42, price_idx=100,
                       quantity=1500, symbol_id=1, seq_num=7)
    print(f"  Encoded: {raw.hex()} ({len(raw)} bytes)")
    decoded = decode_order(raw)
    print_order(decoded, raw)

    # ── Generate test vectors ──
    print("\n-- L3 Test Vector Generation --")
    orders = generate_l3_test_vectors()
    print(f"  Generated {len(orders)} test vectors")

    print("\n  First 10 orders:")
    for i, o in enumerate(orders[:10]):
        if o["msg_type"] not in (MSG_ADD, MSG_CANCEL, MSG_MODIFY):
            print(f"  [{i:3d}] {o['label']}  (invalid, will trigger parse_error)")
            continue
        raw = encode_order(o["msg_type"], o["side"], o["order_id"],
                          o["price_idx"], o["quantity"],
                          o["symbol_id"], o["seq_num"])
        decoded = decode_order(raw)
        print(f"  [{i:3d}] ", end="")
        print_order(decoded)

    # -- Write .mem file --
    script_dir = os.path.dirname(os.path.abspath(__file__))
    memfile = os.path.join(script_dir, "test_vectors.mem")
    write_memfile(orders, memfile)

    # -- FIX Tag Reference --
    print("\n-- FIX Protocol Tag Mapping --")
    for tag, field in sorted(FIX_TAG_MAP.items()):
        print(f"  Tag {tag:<3d} -> {field}")

    print("\n" + "=" * 70)
    print("  Done.  test_vectors.mem ready for $readmemh in tb_l3.v")
    print("=" * 70)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NYSE Pillar-inspired binary order encoder for FPGA HFT")
    parser.add_argument("--memfile", type=str, default=None,
                        help="Output .mem file path (default: test_vectors.mem)")
    parser.add_argument("--gen", type=int, default=0,
                        help="Generate N random orders instead of test vectors")
    args = parser.parse_args()

    if args.gen > 0:
        print(f"Generating {args.gen} random orders...")
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
        outfile = args.memfile or "test_vectors.mem"
        write_memfile(orders, outfile)
    else:
        demo()
