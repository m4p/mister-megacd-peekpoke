// Simulation top for the end-to-end test: hps_ext + dashboard_debug +
// dashboard_sdram_port + a behavioural SDRAM port-2 model (same accept/busy
// semantics as rtl/sdram.sv, with pseudo-random port-1 contention).
module sim_top #(parameter STOCK = 0)
(
	input             clk,
	input             io_enable,
	input             io_strobe,
	input      [15:0] io_din,
	output     [15:0] io_dout,
	output            io_claim,
	input             vblank,
	input             rom_download,
	output     [11:0] joy,
	// work RAM backdoor for the harness
	input             bd_we,
	input      [14:0] bd_addr,
	input      [15:0] bd_data
);

wire [35:0] EXT_BUS;
assign EXT_BUS[31:16] = io_din;
assign EXT_BUS[33]    = io_strobe;
assign EXT_BUS[34]    = io_enable;
assign EXT_BUS[35]    = 1'b0;
assign io_dout  = EXT_BUS[15:0];
assign io_claim = EXT_BUS[32];

wire [48:0] cd_out;
wire        ext_enable, ext_strobe;
wire [15:0] ext_din, dash_dout;
wire        dash_claim;

hps_ext hps_ext
(
	.clk_sys(clk), .EXT_BUS(EXT_BUS), .cd_in(49'h0), .cd_out(cd_out),
	.cdda_ready(1'b1), .cd_data_ready(1'b1),
	.ext_enable(ext_enable), .ext_strobe(ext_strobe), .ext_din(ext_din),
	.dash_dout(STOCK ? 16'h0 : dash_dout), .dash_claim(STOCK ? 1'b0 : dash_claim)
);

// gen.sv PAUSED stand-in: follows the request after the bus-idle/phase-slot
// wait (modelled as 50 cycles), like gen_clken's handshake.
wire        pause_req;
reg         frozen = 0;
reg   [5:0] frz_cnt = 0;
always @(posedge clk) begin
	if (pause_req == frozen) frz_cnt <= 0;
	else if (&frz_cnt) frozen <= pause_req;
	else frz_cnt <= frz_cnt + 1'd1;
end

wire        mem_req, mem_we, mem_ack, mem_err;
wire  [1:0] mem_be;
wire  [7:0] mem_space;
wire [15:1] mem_addr;
wire [15:0] mem_wdata, mem_rdata;

dashboard_debug #(.BUILD_ID(32'h01260925), .LEASE_BITS(24), .FEAT_FREEZE(1)) dashboard_debug
(
	.clk(clk), .reset(1'b0),
	.io_enable(ext_enable), .io_strobe(ext_strobe), .io_din(ext_din),
	.io_dout(dash_dout), .io_claim(dash_claim),
	.vblank(vblank), .joy_inject(joy),
	.mem_req(mem_req), .mem_we(mem_we), .mem_be(mem_be), .mem_space(mem_space),
	.mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_rdata(mem_rdata),
	.mem_ack(mem_ack), .mem_err(mem_err), .mem_block(rom_download),
	.pause_req(pause_req), .frozen(frozen)
);

wire [24:1] sdr_addr;
wire [15:0] sdr_din;
wire        sdr_rd, sdr_wrl, sdr_wrh;
reg         sdr_busy = 0;
reg  [15:0] sdr_dout = 0;
wire        port_busy;

dashboard_sdram_port dashboard_sdram_port
(
	.clk(clk), .reset(1'b0),
	.req(mem_req), .we(mem_we), .be(mem_be), .space(mem_space), .addr(mem_addr),
	.wdata(mem_wdata), .rdata(mem_rdata), .ack(mem_ack), .err(mem_err),
	.port_free(~rom_download), .abort(rom_download), .busy(port_busy), .owns(),
	.sdr_addr(sdr_addr), .sdr_din(sdr_din), .sdr_rd(sdr_rd), .sdr_wrl(sdr_wrl), .sdr_wrh(sdr_wrh),
	.sdr_busy(sdr_busy), .sdr_dout(sdr_dout)
);

reg [15:0] wram[0:32767];
reg        old_rd = 0, old_wr = 0, pending = 0, pend_we = 0;
reg  [2:0] wait_cnt = 0, busy_cnt = 0;
reg [24:1] pend_addr;
reg [15:0] lfsr = 16'hACE1;
wire       rd = sdr_rd & ~rom_download;
wire       wr = sdr_wrl | sdr_wrh;

always @(posedge clk) begin
	lfsr <= {lfsr[14:0], lfsr[15] ^ lfsr[13] ^ lfsr[12] ^ lfsr[10]};
	if(bd_we) wram[bd_addr] <= bd_data;
	old_rd <= old_rd & rd;
	old_wr <= old_wr & wr;
	if(!pending && !sdr_busy && ((~old_rd && rd) || (~old_wr && wr))) begin
		pending   <= 1;
		pend_we   <= wr;
		pend_addr <= sdr_addr;
		wait_cnt  <= lfsr[2:0];
		old_rd    <= rd;
		old_wr    <= wr;
	end
	else if(pending && !sdr_busy) begin
		if(wait_cnt != 0) wait_cnt <= wait_cnt - 1'd1;
		else begin sdr_busy <= 1; busy_cnt <= 3'd5; end
	end
	else if(sdr_busy) begin
		if(busy_cnt > 1) busy_cnt <= busy_cnt - 1'd1;
		else begin
			sdr_busy <= 0;
			pending  <= 0;
			if(!pend_we && pend_addr[24:16] == 9'b010000000) sdr_dout <= wram[pend_addr[15:1]];
		end
	end
	else if(lfsr[1:0] == 0) sdr_dout <= lfsr;   // shared dout clobbered by other ports
end

endmodule
