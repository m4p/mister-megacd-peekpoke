//
// dashboard_debug.sv
//
// Desert Bus dashboard control endpoint for the Mega CD core.
// Wire protocol: docs/dashboard-protocol.md (version 1). Keep the constants
// below in sync with linux/dashboard-bridge/src/protocol.h.
//
// Owns: HPS command 0x70 decode, 2 KiB staging RAM, one bounded memory
// operation at a time, injected player-1 input, and the owner lease.
// It does not freeze the console; FREEZE-dependent features are gated by
// parameters and refused with ERR_UNSUPPORTED when absent.
//
// This source file is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//

module dashboard_debug #(
	parameter [31:0] BUILD_ID          = 32'h0,
	parameter        FEAT_WORKRAM_WRITE = 0,
	parameter        FEAT_VRAM_READ     = 0,
	parameter        FEAT_VRAM_WRITE    = 0,
	parameter        FEAT_FREEZE        = 0,
	parameter        LEASE_BITS         = 26
)
(
	input             clk,
	input             reset,

	// HPS extension bus (see hps_ext.v)
	input             io_enable,
	input             io_strobe,
	input      [15:0] io_din,
	output reg [15:0] io_dout,
	output reg        io_claim,

	// timing
	input             vblank,

	// injected player-1 buttons (active high, core JOY layout)
	output reg [11:0] joy_inject,

	// memory port (clk domain, request held until ack/err)
	output reg        mem_req,
	output reg        mem_we,
	output reg  [1:0] mem_be,      // [1] = even (high) byte, [0] = odd (low) byte
	output reg  [7:0] mem_space,
	output reg [15:1] mem_addr,
	output reg [15:0] mem_wdata,
	input      [15:0] mem_rdata,
	input             mem_ack,
	input             mem_err,     // request dropped before the memory accepted it
	input             mem_block,   // memory unavailable (ROM download): abort between accesses

	// freeze controller (unused unless FEAT_FREEZE)
	output reg        pause_req,
	input             frozen
);

localparam [15:0] CMD_DASH      = 16'h0070;
localparam [15:0] MAGIC_ACK     = 16'hDB26;
localparam [15:0] PROTO_VERSION = 16'd1;
localparam        STAGING_BYTES = 2048;
localparam        MAX_XFER_WORDS = 32;

// subcommands
localparam [7:0] SC_PROBE     = 8'h01;
localparam [7:0] SC_STATUS    = 8'h02;
localparam [7:0] SC_BEGIN     = 8'h03;
localparam [7:0] SC_UPLOAD    = 8'h04;
localparam [7:0] SC_COMMIT    = 8'h05;
localparam [7:0] SC_FETCH     = 8'h06;
localparam [7:0] SC_ACK       = 8'h07;
localparam [7:0] SC_CANCEL    = 8'h08;
localparam [7:0] SC_INPUT     = 8'h09;
localparam [7:0] SC_HEARTBEAT = 8'h0A;
localparam [7:0] SC_PAUSE     = 8'h0B;
localparam [7:0] SC_RESUME    = 8'h0C;
localparam [7:0] SC_SESSION   = 8'h0F;

// result codes
localparam [15:0] R_OK          = 16'd0;
localparam [15:0] R_SESSION     = 16'd1;
localparam [15:0] R_BUSY        = 16'd2;
localparam [15:0] R_TXN         = 16'd3;
localparam [15:0] R_RANGE       = 16'd4;
localparam [15:0] R_UNSUPPORTED = 16'd5;
localparam [15:0] R_INCOMPLETE  = 16'd6;
localparam [15:0] R_STATE       = 16'd7;
localparam [15:0] R_DUP         = 16'd8;
localparam [15:0] R_ABORTED     = 16'd9;
localparam [15:0] R_FAULT       = 16'd10;

// ops / spaces
localparam [7:0] OP_READ     = 8'h01;
localparam [7:0] OP_WRITE    = 8'h02;
localparam [7:0] OP_LOOPBACK = 8'h7F;
localparam [7:0] SP_WORKRAM  = 8'h01;
localparam [7:0] SP_VRAM     = 8'h02;

localparam [15:0] FEATURES = {
	7'd0,
	1'b0,                      // 8 READ_COHERENT (needs freeze)
	1'b1,                      // 7 LOOPBACK
	1'b0,                      // 6 STATE
	FEAT_FREEZE     ? 1'b1 : 1'b0, // 5
	FEAT_VRAM_WRITE ? 1'b1 : 1'b0, // 4
	FEAT_VRAM_READ  ? 1'b1 : 1'b0, // 3
	FEAT_WORKRAM_WRITE ? 1'b1 : 1'b0, // 2
	1'b1,                      // 1 WORKRAM_READ
	1'b1                       // 0 INPUT
};

// transaction states
localparam [1:0] T_IDLE = 2'd0, T_STAGING = 2'd1, T_RUNNING = 2'd2, T_DONE = 2'd3;

//------------------------------------------------------------------
// Staging RAM: two 1024 x 8 true dual-port RAMs (even / odd bytes).
// Port A: SPI side, word addressed. Port B: engine side, byte addressed.
//------------------------------------------------------------------
reg  [9:0] a_addr;
reg [15:0] a_wdata;
reg        a_we;
reg  [7:0] a_qe, a_qo;

reg [10:0] b_addr;      // byte index
reg  [7:0] b_wdata;
reg        b_we;
reg  [7:0] b_qe, b_qo;
reg        b_addr_d;
wire [7:0] b_q = b_addr_d ? b_qo : b_qe;

// Intel's single-clock true dual-port template: a read during a write on the
// same port returns the new data. (Old-data behaviour is not supported by
// Cyclone V M10K in true dual-port mode, and Quartus then builds the array
// from registers.) No caller reads a port while writing it.
(* ramstyle = "M10K" *) reg [7:0] stg_e[0:1023];
(* ramstyle = "M10K" *) reg [7:0] stg_o[0:1023];

wire [9:0] b_idx = b_addr[10:1];

always @(posedge clk) begin
	if(a_we) begin
		stg_e[a_addr] <= a_wdata[15:8];
		a_qe <= a_wdata[15:8];
	end
	else a_qe <= stg_e[a_addr];
end
always @(posedge clk) begin
	if(b_we & ~b_addr[0]) begin
		stg_e[b_idx] <= b_wdata;
		b_qe <= b_wdata;
	end
	else b_qe <= stg_e[b_idx];
end
always @(posedge clk) begin
	if(a_we) begin
		stg_o[a_addr] <= a_wdata[7:0];
		a_qo <= a_wdata[7:0];
	end
	else a_qo <= stg_o[a_addr];
end
always @(posedge clk) begin
	if(b_we & b_addr[0]) begin
		stg_o[b_idx] <= b_wdata;
		b_qo <= b_wdata;
	end
	else b_qo <= stg_o[b_idx];
end
always @(posedge clk) b_addr_d <= b_addr[0];

//------------------------------------------------------------------
// Transaction / engine / input state
//------------------------------------------------------------------
reg  [7:0] gen = 0;
reg  [1:0] tstate = T_IDLE;
reg [15:0] cur_txn;
reg  [7:0] cur_op;
reg  [7:0] cur_space;
reg [15:0] cur_addr;
reg [11:0] cur_len;         // 1..2048
reg [10:0] up_ptr;          // words staged
reg [15:0] res_code;
reg [11:0] res_len;
reg        discard;         // session changed while running
reg        fault = 0;

reg [11:0] inp_target = 0;
reg [11:0] inp_latch  = 0;

reg [LEASE_BITS-1:0] lease = 0;
wire owner = |lease;

reg [15:0] frame_cnt = 0;

wire [10:0] need_words = cur_len[11:1] + cur_len[0];
wire [10:0] res_words  = res_len[11:1] + res_len[0];

// SPI packet parser state
reg  [5:0] wcnt;            // saturating word index (only 0..~40 matter)
reg        active;
reg  [7:0] subcmd;
reg        sess_ok;
reg [15:0] h2, h3, h4, h5;
reg        xfer_ok;
reg  [5:0] xfer_left;

wire [15:0] flags = {gen, |inp_target | |joy_inject, owner, fault, frozen, pause_req,
                     tstate == T_STAGING, tstate == T_DONE, tstate == T_RUNNING};

// engine
localparam [2:0] E_IDLE = 3'd0, E_NEXT = 3'd1, E_RMEM = 3'd2, E_STG = 3'd3,
                 E_WSTG = 3'd4, E_WSTG2 = 3'd5, E_WMEM = 3'd6, E_FIN = 3'd7;
reg  [2:0] estate = E_IDLE;
reg [11:0] eidx;
reg [15:0] eword;           // cached word (READ)
reg [15:1] eword_addr;
reg        eword_valid;
reg        emutated;
wire [15:0] ebyte_addr = cur_addr + {4'd0, eidx};

// vblank edge
reg [2:0] vbl_sr;
wire frame_edge = vbl_sr[1] & ~vbl_sr[2];

function [15:0] begin_check;
	input [15:0] op_space, ahi, alo, len;
	input [1:0]  st;
	reg   [7:0]  op, sp;
	reg   [16:0] end_addr;
	begin
		op = op_space[15:8];
		sp = op_space[7:0];
		end_addr = {1'b0, alo} + {1'b0, len};
		if(st == T_RUNNING || st == T_DONE)                  begin_check = R_BUSY;
		else if(len == 0 || len > STAGING_BYTES)             begin_check = R_RANGE;
		else if(op == OP_LOOPBACK)                           begin_check = R_OK;
		else if(ahi != 0 || end_addr > 17'h10000)            begin_check = R_RANGE;
		else if(op == OP_READ  && sp == SP_WORKRAM)          begin_check = R_OK;
		else if(op == OP_WRITE && sp == SP_WORKRAM)          begin_check = FEAT_WORKRAM_WRITE ? R_OK : R_UNSUPPORTED;
		else if(op == OP_READ  && sp == SP_VRAM)             begin_check = FEAT_VRAM_READ     ? R_OK : R_UNSUPPORTED;
		else if(op == OP_WRITE && sp == SP_VRAM)             begin_check = FEAT_VRAM_WRITE    ? R_OK : R_UNSUPPORTED;
		else                                                 begin_check = R_UNSUPPORTED;
	end
endfunction

always @(posedge clk) begin
	reg [15:0] code;

	a_we <= 0;
	b_we <= 0;
	vbl_sr <= {vbl_sr[1:0], vblank};

	//--------------------------------------------------------------
	// frame boundary: apply input
	//--------------------------------------------------------------
	if(frame_edge) frame_cnt <= frame_cnt + 1'd1;
	if(frame_edge | frozen) begin
		joy_inject <= inp_target | inp_latch;
		inp_latch  <= 0;
	end

	//--------------------------------------------------------------
	// owner lease
	//--------------------------------------------------------------
	if(lease != 0) begin
		lease <= lease - 1'd1;
		if(lease == 1) begin
			inp_target <= 0;
			inp_latch  <= 0;
			joy_inject <= 0;
			pause_req  <= 0;
			if(tstate == T_STAGING) tstate <= T_IDLE;
		end
	end

	//--------------------------------------------------------------
	// engine
	//--------------------------------------------------------------
	case(estate)
		E_IDLE: ;

		E_NEXT:
			if(eidx == cur_len) estate <= E_FIN;
			else if(mem_block) begin
				res_code <= emutated ? R_FAULT : R_ABORTED;
				if(emutated) fault <= 1;
				res_len  <= 0;
				estate   <= E_FIN;
			end
			else if(cur_op == OP_READ) begin
				if(eword_valid && eword_addr == ebyte_addr[15:1]) estate <= E_STG;
				else begin
					mem_req   <= 1;
					mem_we    <= 0;
					mem_be    <= 2'b11;
					mem_space <= cur_space;
					mem_addr  <= ebyte_addr[15:1];
					estate    <= E_RMEM;
				end
			end
			else begin
				b_addr <= eidx[10:0];
				estate <= E_WSTG;
			end

		E_RMEM:
			if(mem_ack | mem_err) begin
				mem_req <= 0;
				if(mem_err) begin
					res_code <= R_ABORTED;
					res_len  <= 0;
					estate   <= E_FIN;
				end
				else begin
					eword       <= mem_rdata;
					eword_addr  <= mem_addr;
					eword_valid <= 1;
					estate      <= E_STG;
				end
			end

		E_STG: begin
				b_addr  <= eidx[10:0];
				b_wdata <= ebyte_addr[0] ? eword[7:0] : eword[15:8];
				b_we    <= 1;
				eidx    <= eidx + 1'd1;
				estate  <= E_NEXT;
			end

		// b_addr was set in E_NEXT; the RAM registers it on the E_WSTG edge,
		// so b_q is valid in E_WSTG2.
		E_WSTG: estate <= E_WSTG2;

		E_WSTG2: begin
				mem_req   <= 1;
				mem_we    <= 1;
				mem_be    <= ebyte_addr[0] ? 2'b01 : 2'b10;
				mem_space <= cur_space;
				mem_addr  <= ebyte_addr[15:1];
				mem_wdata <= {b_q, b_q};
				estate    <= E_WMEM;
			end

		E_WMEM:
			if(mem_ack | mem_err) begin
				mem_req <= 0;
				if(mem_err) begin
					res_code <= emutated ? R_FAULT : R_ABORTED;
					if(emutated) fault <= 1;
					res_len  <= 0;
					estate   <= E_FIN;
				end
				else begin
					emutated <= 1;
					eidx     <= eidx + 1'd1;
					estate   <= E_NEXT;
				end
			end

		E_FIN: begin
				estate <= E_IDLE;
				tstate <= discard ? T_IDLE : T_DONE;
				discard <= 0;
			end

		default: estate <= E_IDLE;
	endcase

	//--------------------------------------------------------------
	// SPI packet parser
	//--------------------------------------------------------------
	if(~io_enable) begin
		wcnt     <= 0;
		active   <= 0;
		io_claim <= 0;
		io_dout  <= 0;
		xfer_ok  <= 0;
	end
	else if(io_strobe) begin
		if(~&wcnt) wcnt <= wcnt + 1'd1;
		io_dout <= 0;

		if(wcnt == 0) begin
			active   <= (io_din == CMD_DASH);
			io_claim <= (io_din == CMD_DASH);
			if(io_din == CMD_DASH) io_dout <= MAGIC_ACK;
		end
		else if(active) begin
			if(wcnt == 1) begin
				subcmd  <= io_din[15:8];
				sess_ok <= (io_din[7:0] == gen) || io_din[15:8] == SC_PROBE || io_din[15:8] == SC_SESSION;
				io_dout <= flags;
				if(io_din[7:0] == gen && gen != 0) lease <= {LEASE_BITS{1'b1}};
			end
			else if(!sess_ok) io_dout <= R_SESSION;
			else case(subcmd)
				SC_PROBE:
					case(wcnt)
						2:  io_dout <= 16'h4442;
						3:  io_dout <= 16'h4D43;
						4:  io_dout <= PROTO_VERSION;
						5:  io_dout <= FEATURES;
						6:  io_dout <= STAGING_BYTES[15:0];
						7:  io_dout <= MAX_XFER_WORDS[15:0];
						8:  io_dout <= BUILD_ID[15:0];
						9:  io_dout <= BUILD_ID[31:16];
						10: io_dout <= {8'd0, gen};
						default: ;
					endcase

				SC_SESSION:
					if(wcnt == 2) begin
						gen        <= io_din[7:0];
						lease      <= (io_din[7:0] != 0) ? {LEASE_BITS{1'b1}} : {LEASE_BITS{1'b0}};
						inp_target <= 0;
						inp_latch  <= 0;
						joy_inject <= 0;
						pause_req  <= 0;
						if(estate == E_IDLE || estate == E_FIN) begin
							tstate  <= T_IDLE;   // overrides E_FIN's T_DONE
							discard <= 0;
						end
						else discard <= 1;       // running: finish, then drop the result
						io_dout <= {8'd0, io_din[7:0]};
					end

				SC_STATUS:
					case(wcnt)
						2: io_dout <= cur_txn;
						3: io_dout <= res_code;
						4: io_dout <= {4'd0, res_len};
						5: io_dout <= {4'd0, joy_inject};
						6: io_dout <= frame_cnt;
						default: ;
					endcase

				SC_BEGIN:
					case(wcnt)
						2: h2 <= io_din;
						3: h3 <= io_din;
						4: h4 <= io_din;
						5: h5 <= io_din;
						6: begin
								code = (h2 == 0) ? R_TXN : begin_check(h3, h4, h5, io_din, tstate);
								io_dout <= code;
								if(code == R_OK) begin
									tstate    <= T_STAGING;
									cur_txn   <= h2;
									cur_op    <= h3[15:8];
									cur_space <= h3[7:0];
									cur_addr  <= h5;
									cur_len   <= io_din[11:0];
									up_ptr    <= 0;
									res_code  <= R_OK;
									res_len   <= 0;
								end
							end
						default: ;
					endcase

				SC_UPLOAD:
					if(wcnt == 2) h2 <= io_din;
					else if(wcnt == 3) h3 <= io_din;
					else if(wcnt == 4) begin
						if(tstate != T_STAGING || cur_op == OP_READ) code = R_STATE;
						else if(h2 != cur_txn) code = R_TXN;
						else if(h3[10:0] != up_ptr || h3[15:11] != 0 || io_din == 0 || io_din > MAX_XFER_WORDS
						        || ({5'd0, h3[10:0]} + io_din) > {5'd0, need_words}) code = R_RANGE;
						else code = R_OK;
						io_dout   <= code;
						xfer_ok   <= (code == R_OK);
						xfer_left <= io_din[5:0];
					end
					else if(xfer_ok && xfer_left != 0 && tstate == T_STAGING) begin
						a_addr    <= up_ptr[9:0];
						a_wdata   <= io_din;
						a_we      <= 1;
						up_ptr    <= up_ptr + 1'd1;
						xfer_left <= xfer_left - 1'd1;
					end

				SC_COMMIT:
					if(wcnt == 2) begin
						if(io_din != cur_txn || tstate == T_IDLE) code = (tstate == T_IDLE) ? R_STATE : R_TXN;
						else if(tstate == T_RUNNING || tstate == T_DONE) code = R_DUP;
						else if(cur_op != OP_READ && up_ptr != need_words) code = R_INCOMPLETE;
						else code = R_OK;
						io_dout <= code;
						if(code == R_OK) begin
							tstate      <= T_RUNNING;
							eidx        <= 0;
							eword_valid <= 0;
							emutated    <= 0;
							res_code    <= R_OK;
							res_len     <= cur_len;
							estate      <= (cur_op == OP_LOOPBACK) ? E_FIN : E_NEXT;
						end
					end

				SC_FETCH:
					if(wcnt == 2) h2 <= io_din;
					else if(wcnt == 3) begin
						h3     <= io_din;
						a_addr <= io_din[9:0];
					end
					else if(wcnt == 4) begin
						if(tstate != T_DONE) code = R_STATE;
						else if(h2 != cur_txn) code = R_TXN;
						else if(h3[15:11] != 0 || io_din == 0 || io_din > MAX_XFER_WORDS
						        || ({5'd0, h3[10:0]} + io_din) > {5'd0, res_words}) code = R_RANGE;
						else code = R_OK;
						io_dout   <= code;
						xfer_ok   <= (code == R_OK);
						xfer_left <= io_din[5:0];
					end
					else if(xfer_ok && xfer_left != 0) begin
						io_dout   <= {a_qe, a_qo};
						a_addr    <= a_addr + 1'd1;
						xfer_left <= xfer_left - 1'd1;
					end

				SC_ACK, SC_CANCEL:
					if(wcnt == 2) begin
						if(tstate == T_IDLE) code = R_STATE;
						else if(io_din != cur_txn) code = R_TXN;
						else if(tstate == T_RUNNING) code = R_BUSY;
						else if(subcmd == SC_ACK && tstate != T_DONE) code = R_STATE;
						else code = R_OK;
						io_dout <= code;
						if(code == R_OK) tstate <= T_IDLE;
					end

				SC_INPUT:
					if(wcnt == 2) h2 <= io_din;
					else if(wcnt == 3) begin
						inp_target <= (inp_target | h2[11:0]) & ~io_din[11:0];
						inp_latch  <= inp_latch | h2[11:0];
						io_dout    <= {4'd0, (inp_target | h2[11:0]) & ~io_din[11:0]};
					end

				SC_HEARTBEAT: ;

				SC_PAUSE, SC_RESUME:
					if(wcnt == 2) begin
						if(!FEAT_FREEZE) io_dout <= R_UNSUPPORTED;
						else begin
							pause_req <= (subcmd == SC_PAUSE);
							io_dout   <= R_OK;
						end
					end

				default: io_dout <= R_UNSUPPORTED;
			endcase
		end
	end

	if(reset) begin
		gen        <= 0;
		tstate     <= T_IDLE;
		estate     <= E_IDLE;
		lease      <= 0;
		inp_target <= 0;
		inp_latch  <= 0;
		joy_inject <= 0;
		pause_req  <= 0;
		mem_req    <= 0;
		discard    <= 0;
		fault      <= 0;
		cur_txn    <= 0;
		res_code   <= 0;
		res_len    <= 0;
	end
end

endmodule
