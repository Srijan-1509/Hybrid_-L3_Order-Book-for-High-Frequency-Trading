// =============================================================================
// axi_to_order_bridge.v — Reads 96-bit orders from BRAM, drives L3 pipeline
//
// Orders are stored in BRAM as 3 consecutive 32-bit words (little-endian):
//   Word 0 (addr+0): msg_data[31:0]    — byte0(msg_type|side), symbol_id, order_id
//   Word 1 (addr+4): msg_data[63:32]   — price_idx, seq_num
//   Word 2 (addr+8): msg_data[95:64]   — quantity
//
// Control interface (from AXI GPIO):
//   ctrl_word[0]     = start pulse (write 1 to begin, edge-detected)
//   ctrl_word[16:1]  = number of orders to process
//
// Operation:
//   1. PS writes orders to BRAM via AXI Port A
//   2. PS writes ctrl_word with start=1 and order_count
//   3. This FSM reads orders from BRAM Port B, one at a time
//   4. Each order is held on msg_data/msg_valid for 1 cycle
//   5. 25-cycle drain between orders (full pipeline flush)
//   6. done flag asserted when all orders are processed
//
// Deterministic latency: 28 cycles per order (3 read + 25 drain)
//
// Target: Xilinx UltraScale Zynq, 250 MHz
// =============================================================================

`timescale 1ns / 1ps

module axi_to_order_bridge #(
    parameter BRAM_ADDR_WIDTH = 12,
    parameter MSG_WIDTH       = 96
)(
    input  wire        clk,
    input  wire        reset,

    // Control (from AXI GPIO)
    input  wire [31:0] ctrl_word,       // [0]=start, [16:1]=order_count

    // BRAM Port B read interface
    output reg  [BRAM_ADDR_WIDTH-1:0] bram_addr,
    output reg                        bram_en,
    input  wire [31:0]                bram_dout,

    // Output to L3 order book
    output reg  [MSG_WIDTH-1:0]       msg_data,
    output reg                        msg_valid,
    input  wire                       msg_ready,

    // Status
    output reg                        busy,
    output reg                        done
);

    // =========================================================================
    // FSM STATES
    // =========================================================================

    localparam S_IDLE     = 3'd0;
    localparam S_READ_W0  = 3'd1;  // Issue BRAM read for word 0
    localparam S_READ_W1  = 3'd2;  // Issue BRAM read for word 1, latch word 0
    localparam S_READ_W2  = 3'd3;  // Issue BRAM read for word 2, latch word 1
    localparam S_LATCH_W2 = 3'd4;  // Latch word 2 from BRAM output
    localparam S_SEND     = 3'd5;  // Drive msg_valid for 1 cycle
    localparam S_DRAIN    = 3'd6;  // Wait for pipeline to process (25 cycles)
    localparam S_DONE     = 3'd7;

    // =========================================================================
    // REGISTERS
    // =========================================================================

    reg [2:0]  state;
    reg [15:0] order_count;
    reg [15:0] order_idx;
    reg [31:0] word0, word1, word2;
    reg [BRAM_ADDR_WIDTH-1:0] base_addr;
    reg [7:0]  drain_cnt;

    // Start edge detection
    reg start_prev;
    wire start_pulse = ctrl_word[0] & ~start_prev;

    always @(posedge clk) begin
        if (reset)
            start_prev <= 1'b0;
        else
            start_prev <= ctrl_word[0];
    end

    // =========================================================================
    // FSM
    // =========================================================================

    always @(posedge clk) begin
        if (reset) begin
            state       <= S_IDLE;
            msg_valid   <= 1'b0;
            bram_en     <= 1'b0;
            busy        <= 1'b0;
            done        <= 1'b0;
            order_idx   <= 16'd0;
            order_count <= 16'd0;
            base_addr   <= {BRAM_ADDR_WIDTH{1'b0}};
            drain_cnt   <= 8'd0;
        end else begin
            case (state)

                S_IDLE: begin
                    msg_valid <= 1'b0;
                    done      <= 1'b0;
                    if (start_pulse) begin
                        order_count <= ctrl_word[16:1];
                        order_idx   <= 16'd0;
                        base_addr   <= {BRAM_ADDR_WIDTH{1'b0}};
                        busy        <= 1'b1;
                        state       <= S_READ_W0;
                    end
                end

                S_READ_W0: begin
                    // Issue BRAM read for word 0 (msg_data[31:0])
                    bram_addr <= base_addr;
                    bram_en   <= 1'b1;
                    state     <= S_READ_W1;
                end

                S_READ_W1: begin
                    // BRAM output now has word 0 (1-cycle read latency)
                    word0     <= bram_dout;
                    // Issue read for word 1
                    bram_addr <= base_addr + {{(BRAM_ADDR_WIDTH-2){1'b0}}, 2'd1};
                    state     <= S_READ_W2;
                end

                S_READ_W2: begin
                    // BRAM output now has word 1
                    word1     <= bram_dout;
                    // Issue read for word 2
                    bram_addr <= base_addr + {{(BRAM_ADDR_WIDTH-2){1'b0}}, 2'd2};
                    state     <= S_LATCH_W2;
                end

                S_LATCH_W2: begin
                    // BRAM output now has word 2
                    word2   <= bram_dout;
                    bram_en <= 1'b0;
                    state   <= S_SEND;
                end

                S_SEND: begin
                    // Assemble and present the 96-bit message
                    msg_data  <= {word2, word1, word0};  // [95:64],[63:32],[31:0]
                    msg_valid <= 1'b1;
                    state     <= S_DRAIN;
                    drain_cnt <= 8'd0;
                end

                S_DRAIN: begin
                    // Deassert msg_valid and wait for pipeline to fully process
                    msg_valid <= 1'b0;
                    drain_cnt <= drain_cnt + 8'd1;
                    if (drain_cnt >= 8'd24) begin
                        // Move to next order
                        order_idx <= order_idx + 16'd1;
                        base_addr <= base_addr + {{(BRAM_ADDR_WIDTH-2){1'b0}}, 2'd3};
                        if (order_idx + 16'd1 >= order_count)
                            state <= S_DONE;
                        else
                            state <= S_READ_W0;
                    end
                end

                S_DONE: begin
                    busy  <= 1'b0;
                    done  <= 1'b1;
                    state <= S_IDLE;
                end

                default: state <= S_IDLE;

            endcase
        end
    end

endmodule
