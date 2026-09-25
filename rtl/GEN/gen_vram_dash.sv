//
// gen_vram_dash.sv
//
// Port-A side of the four VRAM dpram blocks in gen.sv, shared between the
// VDP's own accesses (vram_req/vram_ack toggle handshake) and the dashboard
// (control milestone step 4). Port B (display fetch) is untouched.
//
// Without dashboard requests this is exactly the original gen.sv logic:
//   address_a = vram_a[15:2], data_a = vram_d byte, wren = we & (ack ^ req) & bank,
//   vram_ack <= vram_req every cycle.
// A dashboard access takes the port only while the VDP has nothing pending
// (req == ack), for one cycle; during that cycle the VDP's write enables are
// masked and its acknowledge is held, so a VDP request arriving meanwhile
// simply completes one cycle later with unchanged timing otherwise.
//
// Byte addressing (canonical VDP order): word = A[15:1]; A[1] selects bank 1/2;
// row = A[15:2]; the even byte is the upper RAM (vram_u*), the odd byte the lower.
//
// Writes bypass the VDP's internal sprite-attribute cache; they are meant for
// pattern data (tiles), not for the sprite table.
//
// This source file is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//

module gen_vram_dash
(
	input             clk,

	// VDP port-A request
	input      [15:1] vram_a,
	input      [15:0] vram_d,
	input             vram_we_u,
	input             vram_we_l,
	input             vram_req,
	output reg        vram_ack = 0,

	// the four RAMs' port A
	output     [13:0] ram_addr,
	output     [15:0] ram_d,
	output            wren_l1,
	output            wren_u1,
	output            wren_l2,
	output            wren_u2,
	input      [15:0] ram_q1,   // {u1, l1}
	input      [15:0] ram_q2,   // {u2, l2}

	// dashboard port (held request, one ack pulse)
	input             dash_req,
	input             dash_we,
	input       [1:0] dash_be,  // [1] = even (upper) byte, [0] = odd (lower) byte
	input      [15:1] dash_a,
	input      [15:0] dash_d,
	output reg [15:0] dash_q,
	output reg        dash_ack = 0
);

localparam [1:0] S_IDLE = 2'd0, S_ACCESS = 2'd1, S_DONE = 2'd2;

reg  [1:0] state = S_IDLE;
reg        active = 0;      // port A carries the dashboard access this cycle
reg [15:1] a;
reg [15:0] d;
reg        we;
reg  [1:0] be;

wire vdp_cycle = vram_ack ^ vram_req;

assign ram_addr = active ? a[15:2] : vram_a[15:2];
assign ram_d    = active ? d       : vram_d;
assign wren_l1  = active ? (we & be[0] & ~a[1]) : (vram_we_l & vdp_cycle & ~vram_a[1]);
assign wren_u1  = active ? (we & be[1] & ~a[1]) : (vram_we_u & vdp_cycle & ~vram_a[1]);
assign wren_l2  = active ? (we & be[0] &  a[1]) : (vram_we_l & vdp_cycle &  vram_a[1]);
assign wren_u2  = active ? (we & be[1] &  a[1]) : (vram_we_u & vdp_cycle &  vram_a[1]);

always @(posedge clk) begin
	dash_ack <= 0;
	if (!active) vram_ack <= vram_req;

	case (state)
		S_IDLE:
			if (dash_req && !dash_ack && !vdp_cycle) begin
				active <= 1;
				a      <= dash_a;
				d      <= dash_d;
				we     <= dash_we;
				be     <= dash_be;
				state  <= S_ACCESS;
			end

		// the RAMs write / register the read on the edge that ends this cycle
		S_ACCESS: begin
				active <= 0;
				state  <= S_DONE;
			end

		// q holds the dashboard read for this cycle (the port is the VDP's again)
		S_DONE: begin
				dash_q   <= a[1] ? ram_q2 : ram_q1;
				dash_ack <= 1;
				state    <= S_IDLE;
			end

		default: state <= S_IDLE;
	endcase
end

endmodule
