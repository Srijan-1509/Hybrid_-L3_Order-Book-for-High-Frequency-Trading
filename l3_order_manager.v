// =============================================================================
// l3_order_manager.v — L3 Order-Level Tracking for Bitmap Order Book
//
// Core L3 logic that sits between the feed parser and the existing bitmap.v.
// Maintains a BRAM-based Order Table (indexed by order_id) and per-price-level
// aggregate quantities.  Computes the correct aggregate qty and feeds it to
// the bitmap's existing L2 interface.
//
// Operations (all deterministic 5-cycle pipeline):
//   ADD:    Store order in table, aggregate += order_qty
//   CANCEL: Look up order by ID, aggregate -= order_qty, invalidate entry
//   MODIFY: Look up order by ID, aggregate += (new_qty - old_qty), update entry
//
// Data hazard forwarding for back-to-back same-order-id or same-price-level
// operations (mirrors bitmap.v's bypass logic).
//
// Storage:
//   Order Table:      65536 x 46 bits  (BRAM, ~6 BRAM36K)
//   Aggregate Qty:    8192 x 32 bits   (BRAM, ~8 BRAM36K)
//     [0..4095]    = BID aggregate qty per price level
//     [4096..8191] = ASK aggregate qty per price level
//
// Target: Xilinx UltraScale Zynq, 250 MHz
// =============================================================================

`timescale 1ns / 1ps

module l3_order_manager #(
    parameter IDX_WIDTH     = 12,
    parameter QTY_WIDTH     = 32,
    parameter ORDER_ID_WIDTH = 16
)(
    input  wire                        clk,
    input  wire                        reset,

    // ── Input from feed_parser ──
    input  wire [2:0]                  in_msg_type,
    input  wire                        in_side,
    input  wire [ORDER_ID_WIDTH-1:0]   in_order_id,
    input  wire [IDX_WIDTH-1:0]        in_price_idx,
    input  wire [QTY_WIDTH-1:0]        in_quantity,
    input  wire                        in_valid,

    // ── Output to bitmap.v ──
    output reg  [IDX_WIDTH-1:0]        out_update_idx,
    output reg  [QTY_WIDTH-1:0]        out_update_qty,
    output reg                         out_update_valid,
    output reg                         out_side,

    // ── Status ──
    output reg                         error_dup_add,     // ADD on already-valid order_id
    output reg                         error_cancel_miss  // CANCEL on invalid order_id
);

    // =========================================================================
    // CONSTANTS
    // =========================================================================

    localparam MSG_ADD    = 3'b001;
    localparam MSG_CANCEL = 3'b010;
    localparam MSG_MODIFY = 3'b011;

    localparam OT_DEPTH  = (1 << ORDER_ID_WIDTH);  // 65536
    localparam AGG_DEPTH = (1 << (IDX_WIDTH + 1));  // 8192 (bid + ask)

    // Order Table entry fields within 46-bit word:
    //   [0]        valid
    //   [1]        side
    //   [13:2]     price_idx
    //   [45:14]    qty
    localparam OT_WIDTH = 1 + 1 + IDX_WIDTH + QTY_WIDTH;  // 46

    // =========================================================================
    // BRAM DECLARATIONS
    // =========================================================================

    // Order Table: indexed by order_id
    (* ram_style = "block" *) reg [OT_WIDTH-1:0] order_table [0:OT_DEPTH-1];

    // Aggregate Quantity: indexed by {side, price_idx}
    // agg_addr = {side, price_idx} = side * 4096 + price_idx
    (* ram_style = "block" *) reg [QTY_WIDTH-1:0] agg_qty [0:AGG_DEPTH-1];

    // =========================================================================
    // HELPER: Pack / Unpack Order Table Entry
    // =========================================================================

    function [OT_WIDTH-1:0] ot_pack;
        input               ot_valid;
        input               ot_side;
        input [IDX_WIDTH-1:0] ot_pidx;
        input [QTY_WIDTH-1:0] ot_qty;
        begin
            ot_pack = {ot_qty, ot_pidx, ot_side, ot_valid};
        end
    endfunction

    // Unpack fields from OT entry
    wire                  ot_rd_valid;
    wire                  ot_rd_side;
    wire [IDX_WIDTH-1:0]  ot_rd_pidx;
    wire [QTY_WIDTH-1:0]  ot_rd_qty;

    reg [OT_WIDTH-1:0] ot_read_data_s2;

    assign ot_rd_valid = ot_read_data_s2[0];
    assign ot_rd_side  = ot_read_data_s2[1];
    assign ot_rd_pidx  = ot_read_data_s2[1+IDX_WIDTH:2];
    assign ot_rd_qty   = ot_read_data_s2[OT_WIDTH-1:1+IDX_WIDTH+1];

    // =========================================================================
    // AGGREGATE ADDRESS HELPER
    // =========================================================================

    function [IDX_WIDTH:0] agg_addr;
        input             a_side;
        input [IDX_WIDTH-1:0] a_pidx;
        begin
            agg_addr = {a_side, a_pidx};
        end
    endfunction

    // =========================================================================
    // PIPELINE REGISTERS
    // =========================================================================

    // ── Stage 1 (S1): Latch + start BRAM reads ──
    reg                        s1_valid;
    reg [2:0]                  s1_msg_type;
    reg                        s1_side;
    reg [ORDER_ID_WIDTH-1:0]   s1_order_id;
    reg [IDX_WIDTH-1:0]        s1_price_idx;
    reg [QTY_WIDTH-1:0]        s1_quantity;

    // ── Stage 2 (S2): BRAM data available, start agg read ──
    reg                        s2_valid;
    reg [2:0]                  s2_msg_type;
    reg                        s2_side;
    reg [ORDER_ID_WIDTH-1:0]   s2_order_id;
    reg [IDX_WIDTH-1:0]        s2_price_idx;
    reg [QTY_WIDTH-1:0]        s2_quantity;
    // ot_read_data_s2 is the BRAM read result (registered output)

    // ── Stage 3 (S3): Agg data available, compute new_agg ──
    reg                        s3_valid;
    reg [2:0]                  s3_msg_type;
    reg                        s3_side;
    reg [ORDER_ID_WIDTH-1:0]   s3_order_id;
    reg [IDX_WIDTH-1:0]        s3_price_idx;
    reg [QTY_WIDTH-1:0]        s3_quantity;
    reg [QTY_WIDTH-1:0]        s3_old_qty;       // from order table (for CANCEL/MODIFY)
    reg [QTY_WIDTH-1:0]        s3_agg_read;      // from agg BRAM
    reg [QTY_WIDTH-1:0]        s3_new_agg;       // computed
    reg                        s3_ot_was_valid;   // was the order table entry valid?

    // ── Stage 4 (S4): Write back to BRAMs ──
    reg                        s4_valid;
    reg [2:0]                  s4_msg_type;
    reg                        s4_side;
    reg [ORDER_ID_WIDTH-1:0]   s4_order_id;
    reg [IDX_WIDTH-1:0]        s4_price_idx;
    reg [QTY_WIDTH-1:0]        s4_quantity;
    reg [QTY_WIDTH-1:0]        s4_new_agg;

    // ── Stage 5 (S5): Drive bitmap outputs ──
    reg                        s5_valid;
    reg                        s5_side;
    reg [IDX_WIDTH-1:0]        s5_price_idx;
    reg [QTY_WIDTH-1:0]        s5_new_agg;

    // =========================================================================
    // FORWARDING LOGIC
    //
    // Same-order-id forwarding:  if S1 reads order_id that S3 or S4 is writing,
    //                            use the in-flight data instead of stale BRAM.
    //
    // Same-agg-addr forwarding:  if S2 reads agg at same {side,price_idx} as
    //                            S4 is writing, use the in-flight new_agg.
    // =========================================================================

    // Forwarded OT entry for S2
    // Compare s1_order_id (the order whose BRAM read we are latching)
    // against s3/s4 (orders that have written or are writing to BRAM)
    reg [OT_WIDTH-1:0] ot_forward_data;
    reg                ot_use_forward;

    always @(*) begin
        ot_use_forward = 1'b0;
        ot_forward_data = {OT_WIDTH{1'b0}};

        // S4 has the most recent write — check it first
        if (s4_valid && s1_valid && (s4_order_id == s1_order_id)) begin
            ot_use_forward = 1'b1;
            case (s4_msg_type)
                MSG_ADD:    ot_forward_data = ot_pack(1'b1, s4_side, s4_price_idx, s4_quantity);
                MSG_CANCEL: ot_forward_data = ot_pack(1'b0, s4_side, s4_price_idx, 0);
                MSG_MODIFY: ot_forward_data = ot_pack(1'b1, s4_side, s4_price_idx, s4_quantity);
                default:    ot_forward_data = {OT_WIDTH{1'b0}};
            endcase
        end
        // S3 is writing this cycle — also forward
        else if (s3_valid && s1_valid && (s3_order_id == s1_order_id)) begin
            ot_use_forward = 1'b1;
            case (s3_msg_type)
                MSG_ADD:    ot_forward_data = ot_pack(1'b1, s3_side, s3_price_idx, s3_quantity);
                MSG_CANCEL: ot_forward_data = ot_pack(1'b0, s3_side, s3_price_idx, 0);
                MSG_MODIFY: ot_forward_data = ot_pack(1'b1, s3_side, s3_price_idx, s3_quantity);
                default:    ot_forward_data = {OT_WIDTH{1'b0}};
            endcase
        end
    end

    // Forwarded aggregate qty for S3
    // Compare s2's agg address (the order whose agg BRAM read is in flight)
    // against s4/s5 (orders that have written or are writing agg BRAM)
    wire [IDX_WIDTH:0] s2_agg_addr = agg_addr(s2_side, s2_price_idx);
    wire [IDX_WIDTH:0] s4_agg_addr = agg_addr(s4_side, s4_price_idx);
    wire [IDX_WIDTH:0] s5_agg_addr = agg_addr(s5_side, s5_price_idx);

    reg [QTY_WIDTH-1:0] agg_forward;
    reg                 agg_use_forward;

    always @(*) begin
        agg_use_forward = 1'b0;
        agg_forward = {QTY_WIDTH{1'b0}};

        // S5 wrote most recently
        if (s5_valid && s2_valid && (s5_agg_addr == s2_agg_addr)) begin
            agg_use_forward = 1'b1;
            agg_forward = s5_new_agg;
        end
        // S4 is about to write
        else if (s4_valid && s2_valid && (s4_agg_addr == s2_agg_addr)) begin
            agg_use_forward = 1'b1;
            agg_forward = s4_new_agg;
        end
    end

    // =========================================================================
    // BRAM READ/WRITE PORTS
    // =========================================================================

    // BRAM read data registers (1-cycle read latency)
    reg [OT_WIDTH-1:0]  ot_bram_rd;
    reg [QTY_WIDTH-1:0] agg_bram_rd;

    // =========================================================================
    // PIPELINE
    // =========================================================================

    // Initialization block for simulation
    integer init_i;
    initial begin
        for (init_i = 0; init_i < OT_DEPTH; init_i = init_i + 1)
            order_table[init_i] = {OT_WIDTH{1'b0}};
        for (init_i = 0; init_i < AGG_DEPTH; init_i = init_i + 1)
            agg_qty[init_i] = {QTY_WIDTH{1'b0}};
    end

    always @(posedge clk) begin
        if (reset) begin
            s1_valid <= 1'b0;
            s2_valid <= 1'b0;
            s3_valid <= 1'b0;
            s4_valid <= 1'b0;
            s5_valid <= 1'b0;
            out_update_valid <= 1'b0;
            error_dup_add    <= 1'b0;
            error_cancel_miss <= 1'b0;
        end else begin

            // =================================================================
            // STAGE 1: Latch inputs + initiate BRAM reads
            // =================================================================
            s1_valid     <= in_valid;
            s1_msg_type  <= in_msg_type;
            s1_side      <= in_side;
            s1_order_id  <= in_order_id;
            s1_price_idx <= in_price_idx;
            s1_quantity  <= in_quantity;

            // Initiate Order Table read (result available in S2)
            if (in_valid)
                ot_bram_rd <= order_table[in_order_id];

            // =================================================================
            // STAGE 2: OT data available, start agg read
            // =================================================================
            s2_valid     <= s1_valid;
            s2_msg_type  <= s1_msg_type;
            s2_side      <= s1_side;
            s2_order_id  <= s1_order_id;
            s2_price_idx <= s1_price_idx;
            s2_quantity  <= s1_quantity;

            // Latch OT read (with forwarding check)
            if (s1_valid)
                ot_read_data_s2 <= ot_use_forward ? ot_forward_data : ot_bram_rd;

            // Initiate Aggregate read
            // For ADD: use incoming side + price_idx
            // For CANCEL/MODIFY: use stored side + price_idx from OT (but we
            //   don't have OT data yet in S1, so use incoming fields — the
            //   feed parser provides price_idx and side for all msg types)
            if (s1_valid)
                agg_bram_rd <= agg_qty[agg_addr(s1_side, s1_price_idx)];

            // =================================================================
            // STAGE 3: Compute new aggregate
            // =================================================================
            s3_valid      <= s2_valid;
            s3_msg_type   <= s2_msg_type;
            s3_side       <= s2_side;
            s3_order_id   <= s2_order_id;
            s3_price_idx  <= s2_price_idx;
            s3_quantity   <= s2_quantity;

            if (s2_valid) begin
                s3_old_qty      <= ot_rd_qty;
                s3_ot_was_valid <= ot_rd_valid;

                // Use forwarded agg if available
                s3_agg_read <= agg_use_forward ? agg_forward : agg_bram_rd;
            end

            // Compute new aggregate (combinatorially, latched into s3_new_agg)
            if (s2_valid) begin
                case (s2_msg_type)
                    MSG_ADD: begin
                        // new_agg = old_agg + order_qty
                        s3_new_agg <= (agg_use_forward ? agg_forward : agg_bram_rd)
                                      + s2_quantity;
                    end
                    MSG_CANCEL: begin
                        // new_agg = old_agg - old_order_qty
                        // Guard against underflow
                        if (ot_rd_valid) begin
                            if ((agg_use_forward ? agg_forward : agg_bram_rd) >= ot_rd_qty)
                                s3_new_agg <= (agg_use_forward ? agg_forward : agg_bram_rd)
                                              - ot_rd_qty;
                            else
                                s3_new_agg <= {QTY_WIDTH{1'b0}};
                        end else begin
                            s3_new_agg <= agg_use_forward ? agg_forward : agg_bram_rd;
                        end
                    end
                    MSG_MODIFY: begin
                        // new_agg = old_agg - old_qty + new_qty
                        if (ot_rd_valid) begin
                            s3_new_agg <= (agg_use_forward ? agg_forward : agg_bram_rd)
                                          - ot_rd_qty + s2_quantity;
                        end else begin
                            s3_new_agg <= agg_use_forward ? agg_forward : agg_bram_rd;
                        end
                    end
                    default: begin
                        s3_new_agg <= agg_use_forward ? agg_forward : agg_bram_rd;
                    end
                endcase
            end

            // =================================================================
            // STAGE 4: Write back to BRAMs
            // =================================================================
            s4_valid     <= s3_valid;
            s4_msg_type  <= s3_msg_type;
            s4_side      <= s3_side;
            s4_order_id  <= s3_order_id;
            s4_price_idx <= s3_price_idx;
            s4_quantity  <= s3_quantity;
            s4_new_agg   <= s3_new_agg;

            // Error flags (active for 1 cycle)
            error_dup_add     <= 1'b0;
            error_cancel_miss <= 1'b0;

            if (s3_valid) begin
                case (s3_msg_type)
                    MSG_ADD: begin
                        // Write new entry to Order Table
                        order_table[s3_order_id] <= ot_pack(
                            1'b1, s3_side, s3_price_idx, s3_quantity);
                        // Write new aggregate
                        agg_qty[agg_addr(s3_side, s3_price_idx)] <= s3_new_agg;
                        // Error: duplicate add
                        if (s3_ot_was_valid)
                            error_dup_add <= 1'b1;
                    end

                    MSG_CANCEL: begin
                        if (s3_ot_was_valid) begin
                            // Invalidate Order Table entry
                            order_table[s3_order_id] <= {OT_WIDTH{1'b0}};
                            // Write new aggregate
                            agg_qty[agg_addr(s3_side, s3_price_idx)] <= s3_new_agg;
                        end else begin
                            error_cancel_miss <= 1'b1;
                        end
                    end

                    MSG_MODIFY: begin
                        if (s3_ot_was_valid) begin
                            // Update Order Table entry with new quantity
                            order_table[s3_order_id] <= ot_pack(
                                1'b1, s3_side, s3_price_idx, s3_quantity);
                            // Write new aggregate
                            agg_qty[agg_addr(s3_side, s3_price_idx)] <= s3_new_agg;
                        end
                        // If not valid, silently ignore (could add error flag)
                    end

                    default: ; // no-op
                endcase
            end

            // =================================================================
            // STAGE 5: Drive bitmap interface
            // =================================================================
            s5_valid     <= s4_valid;
            s5_side      <= s4_side;
            s5_price_idx <= s4_price_idx;
            s5_new_agg   <= s4_new_agg;

            if (s4_valid) begin
                out_update_idx   <= s4_price_idx;
                out_update_qty   <= s4_new_agg;
                out_update_valid <= 1'b1;
                out_side         <= s4_side;
            end else begin
                out_update_valid <= 1'b0;
            end

        end // !reset
    end // always

endmodule
