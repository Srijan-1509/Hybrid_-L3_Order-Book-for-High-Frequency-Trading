// =============================================================================
// feed_parser.v — Fixed-Width Binary Message Deserializer
//
// NYSE Pillar-inspired 96-bit message parser for HFT FPGA order book.
// Extracts order fields via pure wire slicing (zero parsing logic).
// 2-stage pipeline: latch → validate.  Deterministic 2-cycle latency.
//
// Message format (96 bits, little-endian):
//   [2:0]   msg_type    001=ADD, 010=CANCEL, 011=MODIFY
//   [3]     side        0=BID, 1=ASK
//   [7:4]   reserved
//   [15:8]  symbol_id   Instrument ID
//   [31:16] order_id    Unique order identifier
//   [43:32] price_idx   Price level index (0–4095)
//   [47:44] reserved
//   [63:48] sequence_num Sequence number
//   [95:64] quantity    Order quantity
//
// Target: Xilinx UltraScale Zynq, 250 MHz
// =============================================================================

`timescale 1ns / 1ps

module feed_parser #(
    parameter DATA_WIDTH = 96
)(
    input  wire                    clk,
    input  wire                    reset,

    // ── Input: raw 96-bit message ──
    input  wire [DATA_WIDTH-1:0]   msg_data,
    input  wire                    msg_valid,
    output wire                    msg_ready,

    // ── Output: parsed order fields ──
    output reg  [2:0]              msg_type,
    output reg                     side,
    output reg  [7:0]              symbol_id,
    output reg  [15:0]             order_id,
    output reg  [11:0]             price_idx,
    output reg  [15:0]             sequence_num,
    output reg  [31:0]             quantity,
    output reg                     parsed_valid,
    output reg                     parse_error
);

    // =========================================================================
    // CONSTANTS
    // =========================================================================

    localparam MSG_ADD    = 3'b001;
    localparam MSG_CANCEL = 3'b010;
    localparam MSG_MODIFY = 3'b011;

    // =========================================================================
    // STAGE 1: LATCH + EXTRACT (pure wire slicing)
    // =========================================================================

    reg [2:0]   s1_msg_type;
    reg         s1_side;
    reg [7:0]   s1_symbol_id;
    reg [15:0]  s1_order_id;
    reg [11:0]  s1_price_idx;
    reg [15:0]  s1_seq_num;
    reg [31:0]  s1_quantity;
    reg         s1_valid;

    // Always ready to accept (single-cycle latch, no backpressure)
    assign msg_ready = !reset;

    always @(posedge clk) begin
        if (reset) begin
            s1_valid <= 1'b0;
        end else begin
            s1_valid <= msg_valid;
            if (msg_valid) begin
                // Wire slicing — zero logic, just routing
                s1_msg_type  <= msg_data[2:0];
                s1_side      <= msg_data[3];
                s1_symbol_id <= msg_data[15:8];
                s1_order_id  <= msg_data[31:16];
                s1_price_idx <= msg_data[43:32];
                s1_seq_num   <= msg_data[63:48];
                s1_quantity  <= msg_data[95:64];
            end
        end
    end

    // =========================================================================
    // STAGE 2: VALIDATE + OUTPUT
    //
    // Check msg_type is one of {ADD, CANCEL, MODIFY}.
    // On valid message: assert parsed_valid.
    // On invalid message: assert parse_error (parsed_valid stays low).
    // =========================================================================

    wire s1_type_valid = (s1_msg_type == MSG_ADD)    ||
                         (s1_msg_type == MSG_CANCEL) ||
                         (s1_msg_type == MSG_MODIFY);

    always @(posedge clk) begin
        if (reset) begin
            parsed_valid <= 1'b0;
            parse_error  <= 1'b0;
            msg_type     <= 3'b000;
            side         <= 1'b0;
            symbol_id    <= 8'd0;
            order_id     <= 16'd0;
            price_idx    <= 12'd0;
            sequence_num <= 16'd0;
            quantity     <= 32'd0;
        end else begin
            if (s1_valid && s1_type_valid) begin
                // Valid message — pass through
                parsed_valid <= 1'b1;
                parse_error  <= 1'b0;
                msg_type     <= s1_msg_type;
                side         <= s1_side;
                symbol_id    <= s1_symbol_id;
                order_id     <= s1_order_id;
                price_idx    <= s1_price_idx;
                sequence_num <= s1_seq_num;
                quantity     <= s1_quantity;
            end else if (s1_valid && !s1_type_valid) begin
                // Invalid message type
                parsed_valid <= 1'b0;
                parse_error  <= 1'b1;
            end else begin
                parsed_valid <= 1'b0;
                parse_error  <= 1'b0;
            end
        end
    end

endmodule
