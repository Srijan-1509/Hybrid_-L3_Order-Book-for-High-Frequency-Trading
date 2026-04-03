// =============================================================================
// l3_order_book_top.v — Top-Level L3 Order Book Integration
//
// Instantiation chain:
//   feed_parser → l3_order_manager → bitmap (existing, unchanged)
//
// End-to-end deterministic latency:
//   Feed Parser:       2 cycles
//   L3 Order Manager:  5 cycles
//   Bitmap BBO:        9 cycles
//   ─────────────────────────────
//   Total:            16 cycles  (64 ns at 250 MHz)
//
// Target: Xilinx UltraScale Zynq, 250 MHz
// =============================================================================

`timescale 1ns / 1ps

module l3_order_book_top #(
    parameter IDX_WIDTH      = 12,
    parameter QTY_WIDTH      = 32,
    parameter ORDER_ID_WIDTH = 16,
    parameter MSG_WIDTH      = 96
)(
    input  wire                    clk,
    input  wire                    reset,

    // ── Message input (from AXI-Stream or testbench) ──
    input  wire [MSG_WIDTH-1:0]    msg_data,
    input  wire                    msg_valid,
    output wire                    msg_ready,

    // ── Static configuration ──
    input  wire [31:0]             base_price,

    // ── BBO output (from bitmap core) ──
    output wire [31:0]             best_bid_price,
    output wire [31:0]             best_ask_price,
    output wire [QTY_WIDTH-1:0]    best_bid_qty,
    output wire [QTY_WIDTH-1:0]    best_ask_qty,
    output wire                    bbo_valid,

    // ── Feed parser status ──
    output wire                    parse_error,

    // ── L3 manager status ──
    output wire                    error_dup_add,
    output wire                    error_cancel_miss
);

    // =========================================================================
    // INTERNAL WIRES
    // =========================================================================

    // Feed parser → L3 order manager
    wire [2:0]                  fp_msg_type;
    wire                        fp_side;
    wire [7:0]                  fp_symbol_id;
    wire [15:0]                 fp_order_id;
    wire [11:0]                 fp_price_idx;
    wire [15:0]                 fp_sequence_num;
    wire [31:0]                 fp_quantity;
    wire                        fp_parsed_valid;

    // L3 order manager → bitmap
    wire [IDX_WIDTH-1:0]        l3_update_idx;
    wire [QTY_WIDTH-1:0]        l3_update_qty;
    wire                        l3_update_valid;
    wire                        l3_side;

    // =========================================================================
    // FEED PARSER
    // =========================================================================

    feed_parser #(
        .DATA_WIDTH  (MSG_WIDTH)
    ) u_feed_parser (
        .clk          (clk),
        .reset        (reset),

        // Input
        .msg_data     (msg_data),
        .msg_valid    (msg_valid),
        .msg_ready    (msg_ready),

        // Output → L3 manager
        .msg_type     (fp_msg_type),
        .side         (fp_side),
        .symbol_id    (fp_symbol_id),
        .order_id     (fp_order_id),
        .price_idx    (fp_price_idx),
        .sequence_num (fp_sequence_num),
        .quantity     (fp_quantity),
        .parsed_valid (fp_parsed_valid),
        .parse_error  (parse_error)
    );

    // =========================================================================
    // L3 ORDER MANAGER
    // =========================================================================

    l3_order_manager #(
        .IDX_WIDTH      (IDX_WIDTH),
        .QTY_WIDTH      (QTY_WIDTH),
        .ORDER_ID_WIDTH (ORDER_ID_WIDTH)
    ) u_l3_manager (
        .clk              (clk),
        .reset            (reset),

        // Input ← feed parser
        .in_msg_type      (fp_msg_type),
        .in_side          (fp_side),
        .in_order_id      (fp_order_id[ORDER_ID_WIDTH-1:0]),
        .in_price_idx     (fp_price_idx[IDX_WIDTH-1:0]),
        .in_quantity      (fp_quantity),
        .in_valid         (fp_parsed_valid),

        // Output → bitmap
        .out_update_idx   (l3_update_idx),
        .out_update_qty   (l3_update_qty),
        .out_update_valid (l3_update_valid),
        .out_side         (l3_side),

        // Status
        .error_dup_add    (error_dup_add),
        .error_cancel_miss(error_cancel_miss)
    );

    // =========================================================================
    // BITMAP CORE (EXISTING — UNCHANGED)
    // =========================================================================

    bitmap #(
        .IDX_WIDTH (IDX_WIDTH),
        .QTY_WIDTH (QTY_WIDTH)
    ) u_bitmap (
        .clk            (clk),
        .reset          (reset),

        // Input ← L3 manager
        .update_idx     (l3_update_idx),
        .update_qty     (l3_update_qty),
        .update_valid   (l3_update_valid),
        .side           (l3_side),

        // Config
        .base_price     (base_price),

        // Output → BBO
        .best_bid_price (best_bid_price),
        .best_ask_price (best_ask_price),
        .best_bid_qty   (best_bid_qty),
        .best_ask_qty   (best_ask_qty),
        .bbo_valid      (bbo_valid)
    );

endmodule
