//
// hps_ext for Mega CD
//
// Copyright (c) 2020 Alexey Melnikov
//
// This source file is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This source file is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.
//
///////////////////////////////////////////////////////////////////////

module hps_ext
(
	input             clk_sys,
	inout      [35:0] EXT_BUS,

	// CD interface
	input      [48:0] cd_in,
	output reg [48:0] cd_out,

	input             cdda_ready,
	input             cd_data_ready,

	// Dashboard endpoint (command 0x70, see docs/dashboard-protocol.md).
	// Tie dash_claim low when the dashboard is not built in.
	output            ext_enable,
	output            ext_strobe,
	output     [15:0] ext_din,
	input      [15:0] dash_dout,
	input             dash_claim
);

reg [15:0] io_dout;
reg        dout_en = 0;
reg  [9:0] byte_cnt;

// Commands are recognized explicitly; anything else is left to hps_io.
assign EXT_BUS[15:0] = dash_claim ? dash_dout : io_dout;
wire [15:0] io_din = EXT_BUS[31:16];
assign EXT_BUS[32] = dout_en | dash_claim;
wire io_strobe = EXT_BUS[33];
wire io_enable = EXT_BUS[34];

assign ext_enable = io_enable;
assign ext_strobe = io_strobe;
assign ext_din    = io_din;

localparam CD_GET = 'h34;
localparam CD_SET = 'h35;


always@(posedge clk_sys) begin
	reg [15:0] cmd;
	reg  [7:0] cd_req = 0;
	reg        old_cd = 0;
	reg  [1:0] get_cmd;
	reg        send_data_type;

	old_cd <= cd_in[48];
	if(old_cd ^ cd_in[48]) cd_req <= cd_req + 1'd1; 

	if(~io_enable) begin
		dout_en <= 0;
		io_dout <= 0;
		byte_cnt <= 0;
		if(cmd == 'h35) cd_out[48] <= ~cd_out[48]; 
	end
	else if(io_strobe) begin

		io_dout <= 0;
		if(~&byte_cnt) byte_cnt <= byte_cnt + 1'd1;

		if(byte_cnt == 0) begin
			cmd <= io_din;
			dout_en <= (io_din == CD_GET || io_din == CD_SET);
			if(io_din == CD_GET) io_dout <= cd_req; 
		end else begin
			case(cmd)
				CD_GET:
					if(!byte_cnt[9:3]) begin
						if (byte_cnt[2:0] == 1) begin
							get_cmd <= io_din[1:0];
							send_data_type <= io_din[2];
						end else begin
							if (get_cmd == 0) begin // Request Command data
								case(byte_cnt[2:0])
									2: io_dout <= cd_in[15:0];
									3: io_dout <= cd_in[31:16];
									4: io_dout <= cd_in[47:32];
								endcase
							end else if (get_cmd == 1) begin // Request ready for data
								if (byte_cnt[2:0] == 2) begin
									io_dout <= {15'd0, send_data_type ? cd_data_ready : cdda_ready};
								end
							end
						end
					end

				CD_SET:
					if(!byte_cnt[9:3]) begin
						case(byte_cnt[2:0])
							1: cd_out[15:0]  <= io_din;
							2: cd_out[31:16] <= io_din;
							3: cd_out[47:32] <= io_din;
						endcase
					end
			endcase
		end
	end
end

endmodule
