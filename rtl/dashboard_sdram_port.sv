//
// dashboard_sdram_port.sv
//
// Adapts dashboard_debug's held-request memory port to SDRAM port 2 of
// rtl/sdram.sv, using the same request/busy pattern as the tmpram engine in
// MegaCD.sv: raise rd/wr, drop it once busy rises, sample dout on the
// clk_sys cycle where the delayed busy is high and the current busy is low.
//
// Only WORKRAM (space 1) is mapped: byte offset A -> SDRAM byte 0x800000 + A,
// the same location sdram.addr1 uses for GEN_RAM_CE_N.
//
// This source file is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//

module dashboard_sdram_port
(
	input             clk,
	input             reset,

	// from dashboard_debug
	input             req,
	input             we,
	input       [1:0] be,
	input       [7:0] space,
	input      [15:1] addr,
	input      [15:0] wdata,
	output reg [15:0] rdata,
	output reg        ack,
	output reg        err,

	// arbitration with ROM download and the backup-RAM (tmpram) engine
	input             port_free,   // no ROM download, tmpram engine idle
	input             abort,       // ROM download started: drop a not-yet-accepted request
	output            busy,        // look-ahead: owns or is about to own port 2 (gates the tmpram engine)
	output reg        owns = 0,    // registered: port 2 is ours (selects the address/data muxes)

	// SDRAM port 2
	output reg [24:1] sdr_addr,
	output reg [15:0] sdr_din,
	output            sdr_rd,
	output            sdr_wrl,
	output            sdr_wrh,
	input             sdr_busy,
	input      [15:0] sdr_dout
);

localparam [7:0] SP_WORKRAM = 8'h01;

localparam [1:0] S_IDLE = 2'd0, S_ISSUE = 2'd1, S_WAIT = 2'd2;

reg  [1:0] state = S_IDLE;
reg        sig_rd, sig_wrl, sig_wrh;
reg        busy_d;

wire start = (state == S_IDLE) && req && !ack && !err && port_free && space == SP_WORKRAM;

assign busy    = owns | start;
assign sdr_rd  = sig_rd;
assign sdr_wrl = sig_wrl;
assign sdr_wrh = sig_wrh;

always @(posedge clk) begin
	busy_d <= sdr_busy;
	ack    <= 0;
	err    <= 0;

	case(state)
		S_IDLE:
			if(req && !ack && !err && (space != SP_WORKRAM || abort)) err <= 1; // unmapped / ROM download
			else if(start) begin
				owns     <= 1;
				sdr_addr <= {9'b010000000, addr};
				sdr_din  <= wdata;
				sig_rd   <= ~we;
				sig_wrh  <= we & be[1];
				sig_wrl  <= we & be[0];
				state    <= S_ISSUE;
			end

		S_ISSUE:
			if(~busy_d & sdr_busy) begin
				{sig_rd, sig_wrh, sig_wrl} <= 0;
				state <= S_WAIT;
			end
			else if(abort) begin
				// not accepted yet: withdraw before the ROM loader takes the port
				{sig_rd, sig_wrh, sig_wrl} <= 0;
				owns  <= 0;
				err   <= 1;
				state <= S_IDLE;
			end

		S_WAIT:
			if(busy_d & ~sdr_busy) begin
				rdata <= sdr_dout;
				ack   <= 1;
				owns  <= 0;
				state <= S_IDLE;
			end

		default: state <= S_IDLE;
	endcase

	if(reset) begin
		state <= S_IDLE;
		owns  <= 0;
		{sig_rd, sig_wrh, sig_wrl} <= 0;
	end
end

endmodule
