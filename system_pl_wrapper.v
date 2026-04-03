// =============================================================================
// system_pl_wrapper.v — Top-Level PL Wrapper for ZCU102 Block Design
//
// Integrates:
//   axi_to_order_bridge (BRAM reader FSM)
//   l3_order_book_top   (feed_parser → l3_order_manager → bitmap)
//
// Interfaces:
//   - BRAM Port B (read-only, connected to Block Memory Generator)
//   - AXI GPIO control (start, order_count, base_price)
//   - AXI GPIO status  (BBO prices + quantities)
//   - ILA debug taps   (msg_data, msg_valid, BBO outputs)
//
// Target: Xilinx UltraScale Zynq ZCU102, 250 MHz
// =============================================================================

`timescale 1ns / 1ps

module system_pl_wrapper #(
    parameter IDX_WIDTH      = 12,
    parameter QTY_WIDTH      = 32,
    parameter ORDER_ID_WIDTH = 16,
    parameter MSG_WIDTH      = 96
)(
    input  wire        clk,
    input  wire        reset,

    // ── Control from PS (AXI GPIO) ──
    input  wire [31:0] ctrl_word,       // [0]=start, [16:1]=order_count
    input  wire [31:0] base_price,      // from GPIO channel 2

    // ── BRAM Port B interface ──
    // Directly wired to Block Memory Generator BRAM_PORTB
    output wire [11:0] bram_addr_b,
    output wire        bram_en_b,
    output wire [3:0]  bram_we_b,       // always 0 (read-only from PL)
    input  wire [31:0] bram_dout_b,
    output wire [31:0] bram_din_b,
    output wire        bram_rst_b,
    output wire        bram_clk_b,

    // ── BBO Outputs (to AXI GPIO status) ──
    output wire [31:0] best_bid_price,
    output wire [31:0] best_ask_price,
    output wire [QTY_WIDTH-1:0] best_bid_qty,
    output wire [QTY_WIDTH-1:0] best_ask_qty,
    output wire        bbo_valid,

    // ── Status ──
    output wire        busy,
    output wire        done,
    output wire        parse_error,
    output wire        error_dup_add,
    output wire        error_cancel_miss,

    // ── ILA / Debug probe access ──
    output wire [MSG_WIDTH-1:0] dbg_msg_data,
    output wire                 dbg_msg_valid
);

    // =========================================================================
    // BRAM PORT B — READ-ONLY SIGNALS
    // =========================================================================

    assign bram_we_b  = 4'b0000;        // never write from PL
    assign bram_din_b = 32'd0;           // unused
    assign bram_rst_b = reset;
    assign bram_clk_b = clk;

    // =========================================================================
    // INTERNAL WIRES
    // =========================================================================

    wire [MSG_WIDTH-1:0] msg_data;
    wire                 msg_valid;
    wire                 msg_ready;

    // Debug taps — directly alias internal signals
    assign dbg_msg_data  = msg_data;
    assign dbg_msg_valid = msg_valid;

    // =========================================================================
    // AXI-TO-ORDER BRIDGE
    // =========================================================================

    axi_to_order_bridge #(
        .BRAM_ADDR_WIDTH (12),
        .MSG_WIDTH       (MSG_WIDTH)
    ) u_bridge (
        .clk        (clk),
        .reset      (reset),
        .ctrl_word  (ctrl_word),
        .bram_addr  (bram_addr_b),
        .bram_en    (bram_en_b),
        .bram_dout  (bram_dout_b),
        .msg_data   (msg_data),
        .msg_valid  (msg_valid),
        .msg_ready  (msg_ready),
        .busy       (busy),
        .done       (done)
    );

    // =========================================================================
    // L3 ORDER BOOK
    // =========================================================================

    l3_order_book_top #(
        .IDX_WIDTH      (IDX_WIDTH),
        .QTY_WIDTH      (QTY_WIDTH),
        .ORDER_ID_WIDTH (ORDER_ID_WIDTH),
        .MSG_WIDTH      (MSG_WIDTH)
    ) u_l3_top (
        .clk              (clk),
        .reset            (reset),
        .msg_data         (msg_data),
        .msg_valid        (msg_valid),
        .msg_ready        (msg_ready),
        .base_price       (base_price),
        .best_bid_price   (best_bid_price),
        .best_ask_price   (best_ask_price),
        .best_bid_qty     (best_bid_qty),
        .best_ask_qty     (best_ask_qty),
        .bbo_valid        (bbo_valid),
        .parse_error      (parse_error),
        .error_dup_add    (error_dup_add),
        .error_cancel_miss(error_cancel_miss)
    );

endmodule
