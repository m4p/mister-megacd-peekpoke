-- Testbench for the Mega CD ASIC dashboard freeze (control milestone step 3).
--
-- The real ASIC runs sub-CPU program-RAM reads and writes against an SDRAM
-- port-0 model (edge-triggered accept, busy pulse, shared dout that other ports
-- overwrite afterwards) while FREEZE toggles at random. The sub-CPU model only
-- advances on the ASIC's own S68K_CE pulses, so it stops exactly like FX68K.
-- Checks: every read returns the last value written (shadow memory), the CPU
-- keeps making progress (no lost SDRAM handshake), no SDRAM access starts while
-- FROZEN, and FROZEN is never reported with an access in flight.
--
-- Run: make -C tests/dashboard sim-vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

entity tb_asic_freeze is
	generic (SEED : integer := 1);
end tb_asic_freeze;

architecture sim of tb_asic_freeze is
	signal CLK      : std_logic := '0';
	signal RST_N    : std_logic := '0';
	signal FREEZE   : std_logic := '0';
	signal FROZEN   : std_logic;

	signal S68K_A   : std_logic_vector(23 downto 1) := (others => '0');
	signal S68K_DI  : std_logic_vector(15 downto 0) := (others => '0');
	signal S68K_DO  : std_logic_vector(15 downto 0);
	signal S68K_AS_N, S68K_UDS_N, S68K_LDS_N : std_logic := '1';
	signal S68K_RNW : std_logic := '1';
	signal S68K_DTACK_N : std_logic;
	signal S68K_CE_F, S68K_CE_R : std_logic;

	signal EXT_VA   : std_logic_vector(17 downto 1) := (others => '0');
	signal EXT_VDI  : std_logic_vector(15 downto 0) := (others => '0');
	signal EXT_AS_N, EXT_UDS_N, EXT_LDS_N, EXT_FDC_N : std_logic := '1';
	signal EXT_RNW  : std_logic := '1';
	signal EXT_DTACK_N : std_logic;

	signal PRG_A    : std_logic_vector(17 downto 0);
	signal PRG_DI   : std_logic_vector(15 downto 0) := (others => '0');
	signal PRG_DO   : std_logic_vector(15 downto 0);
	signal PRG_WRL_N, PRG_WRH_N, PRG_OE_N : std_logic;
	signal PRG_RDY  : std_logic := '1';

	-- SDRAM port-0 model
	type mem_t is array (0 to 4095) of std_logic_vector(15 downto 0);
	signal busy     : std_logic := '0';

	signal errors   : integer := 0;
	signal reads, writes, freezes, frozen_cycles : integer := 0;
	signal done     : boolean := false;
begin
	CLK <= not CLK after 9.3 ns when not done else '0';
	PRG_RDY <= not busy;

	dut : entity work.ASIC
	port map(
		CLK => CLK, RST_N => RST_N, ENABLE => '1', FREEZE => FREEZE, FROZEN => FROZEN,
		S68K_A => S68K_A, S68K_DI => S68K_DI, S68K_DO => S68K_DO, S68K_AS_N => S68K_AS_N,
		S68K_RNW => S68K_RNW, S68K_UDS_N => S68K_UDS_N, S68K_LDS_N => S68K_LDS_N,
		S68K_DTACK_N => S68K_DTACK_N, S68K_IPL_N => open, S68K_VPA_N => open, S68K_FC => "01",
		S68K_HALT_N => open, S68K_RESET_N => open, S68K_CE_F => S68K_CE_F, S68K_CE_R => S68K_CE_R,
		EXT_VA => EXT_VA, EXT_VDI => EXT_VDI, EXT_VDO => open, EXT_AS_N => EXT_AS_N, EXT_RNW => EXT_RNW,
		EXT_UDS_N => EXT_UDS_N, EXT_LDS_N => EXT_LDS_N, EXT_DTACK_N => EXT_DTACK_N, EXT_ASEL_N => '1',
		EXT_VCLK_CE => '1', EXT_RAS2_N => '1', EXT_ROM_N => '1', EXT_FDC_N => EXT_FDC_N,
		PRG_A => PRG_A, PRG_DI => PRG_DI, PRG_DO => PRG_DO, PRG_WRL_N => PRG_WRL_N, PRG_WRH_N => PRG_WRH_N,
		PRG_OE_N => PRG_OE_N, PRG_RFS => open, PRG_RDY => PRG_RDY,
		PCM_A => open, PCM_DI => open, PCM_WE_N => open, PCM_N => open,
		ROM_DI => (others => '0'), ROM_CE_N => open, ROM_RDY => '1',
		PRAM_N => open, BRAM_N => open, BROM_N => open, CDC_N => open, COE_N => open,
		CLWE_N => open, CUWE_N => open, CDC_INT_N => '1', ERES_N => open,
		CDC_HDI => (others => '0'), CDC_HRD_N => open, CDC_DTEN_N => '1', CDC_WAIT_N => '1',
		CD_DI => (others => '0'), CD_SC_WR => '0', CDD_STAT => (others => '0'), CDD_COMM => open,
		CDD_SEND => open, CDD_REC => '0', CDD_DM => '0',
		WORDRAM0_A => open, WORDRAM0_DI => (others => '0'), WORDRAM0_DO => open, WORDRAM0_WR => open,
		WORDRAM1_A => open, WORDRAM1_DI => (others => '0'), WORDRAM1_DO => open, WORDRAM1_WR => open,
		FD_DAT => open, FD_WR => open, LED_RED => open, LED_GREEN => open
	);

	-- SDRAM port 0: requests are rising-edge detected (rd = ~OE_N, wr = ~WRx_N);
	-- busy rises on acceptance after 0..5 cycles and falls when dout is loaded;
	-- dout is shared and gets clobbered by other ports afterwards.
	sdram : process(CLK)
		variable mem       : mem_t := (others => (others => '0'));
		variable old_rd, old_wr, pending, pend_we : boolean := false;
		variable pend_a    : integer;
		variable pend_d    : std_logic_vector(15 downto 0);
		variable pend_l, pend_h : std_logic;
		variable wait_c, busy_c : integer := 0;
		variable s1, s2    : positive := SEED;
		variable r         : real;
		variable rd, wr    : boolean;
	begin
		if rising_edge(CLK) then
			rd := PRG_OE_N = '0';
			wr := PRG_WRL_N = '0' or PRG_WRH_N = '0';
			if not rd then old_rd := false; end if;
			if not wr then old_wr := false; end if;
			if not pending and busy = '0' and ((rd and not old_rd) or (wr and not old_wr)) then
				if FROZEN = '1' then
					report "FAIL: SDRAM access started while FROZEN" severity error;
				end if;
				pending := true; pend_we := wr;
				pend_a := to_integer(unsigned(PRG_A(11 downto 0)));
				pend_d := PRG_DO; pend_l := not PRG_WRL_N; pend_h := not PRG_WRH_N;
				uniform(s1, s2, r); wait_c := integer(r * 5.0);
				old_rd := rd; old_wr := wr;
			elsif pending and busy = '0' then
				if wait_c > 0 then wait_c := wait_c - 1;
				else busy <= '1'; busy_c := 5; end if;
			elsif busy = '1' then
				if busy_c > 1 then busy_c := busy_c - 1;
				else
					busy <= '0'; pending := false;
					if pend_we then
						if pend_h = '1' then mem(pend_a)(15 downto 8) := pend_d(15 downto 8); end if;
						if pend_l = '1' then mem(pend_a)(7 downto 0) := pend_d(7 downto 0); end if;
					else
						PRG_DI <= mem(pend_a);
					end if;
				end if;
			else
				uniform(s1, s2, r);
				if r < 0.3 then PRG_DI <= std_logic_vector(to_unsigned(integer(r * 65535.0), 16)); end if;
			end if;
			if FROZEN = '1' and (pending or busy = '1') then
				report "FAIL: FROZEN with an SDRAM access in flight" severity error;
			end if;
		end if;
	end process;

	-- FREEZE: random periods, like repeated dashboard pauses and transaction freezes
	freezer : process
		variable s1, s2 : positive := SEED + 7;
		variable r : real;
		variable n : integer;
	begin
		wait until RST_N = '1';
		wait for 20 us;
		while not done loop
			uniform(s1, s2, r); n := 1 + integer(r * 3000.0);
			for i in 1 to n loop wait until rising_edge(CLK); end loop;
			FREEZE <= '1'; freezes <= freezes + 1;
			uniform(s1, s2, r); n := 1 + integer(r * (1.0 + 1500.0 * r));
			for i in 1 to n loop
				wait until rising_edge(CLK);
				if FROZEN = '1' then frozen_cycles <= frozen_cycles + 1; end if;
			end loop;
			FREEZE <= '0';
		end loop;
		wait;
	end process;

	-- Main CPU: release the sub-CPU (SRES=1, SBRQ=0) through $A12000, then
	-- the sub-CPU model runs program-RAM cycles on its own clock enables.
	stim : process
		variable s1, s2 : positive := SEED + 3;
		variable r      : real;
		variable a      : integer;
		variable d      : std_logic_vector(15 downto 0);
		variable shadow : mem_t := (others => (others => '0'));
		variable written: std_logic_vector(0 to 4095) := (others => '0');
		variable nread, nwrite, guard : integer := 0;

		procedure ce_wait is   -- next sub-CPU phase-1 enable (none arrive while frozen)
		begin
			loop
				wait until rising_edge(CLK);
				exit when S68K_CE_R = '1';
			end loop;
		end procedure;
	begin
		wait for 200 ns;
		RST_N <= '1';
		for i in 1 to 300 loop wait until rising_edge(CLK); end loop;

		EXT_VA <= (others => '0'); EXT_VDI <= x"0001"; EXT_RNW <= '0';
		EXT_FDC_N <= '0'; EXT_LDS_N <= '0'; EXT_UDS_N <= '0'; EXT_AS_N <= '0';
		wait until rising_edge(CLK) and EXT_DTACK_N = '0';
		EXT_AS_N <= '1'; EXT_LDS_N <= '1'; EXT_UDS_N <= '1'; EXT_FDC_N <= '1'; EXT_RNW <= '1';
		for i in 1 to 50 loop wait until rising_edge(CLK); end loop;

		for cyc in 1 to 1500 loop
			uniform(s1, s2, r); a := integer(r * 4095.0);
			uniform(s1, s2, r);
			ce_wait;
			S68K_A <= (others => '0');
			S68K_A(12 downto 1) <= std_logic_vector(to_unsigned(a, 12));
			if r < 0.5 then
				d := std_logic_vector(to_unsigned(cyc * 97 + a, 16));
				S68K_DI <= d; S68K_RNW <= '0';
			else
				S68K_RNW <= '1';
			end if;
			ce_wait;
			S68K_AS_N <= '0'; S68K_LDS_N <= '0'; S68K_UDS_N <= '0';
			guard := 0;
			loop
				ce_wait;
				exit when S68K_DTACK_N = '0';
				guard := guard + 1;
				if guard > 20000 then
					report "FAIL: sub-CPU cycle never acknowledged (lost handshake)" severity failure;
				end if;
			end loop;
			ce_wait;
			if S68K_RNW = '1' then
				nread := nread + 1;
				if written(a) = '1' and S68K_DO /= shadow(a) then
					report "FAIL: read " & integer'image(a) & " returned wrong data" severity error;
				end if;
			else
				nwrite := nwrite + 1;
				shadow(a) := d; written(a) := '1';
			end if;
			S68K_AS_N <= '1'; S68K_LDS_N <= '1'; S68K_UDS_N <= '1'; S68K_RNW <= '1';
			reads <= nread; writes <= nwrite;
		end loop;

		report "DONE: tb_asic_freeze seed=" & integer'image(SEED) & " reads=" & integer'image(nread) &
		       " writes=" & integer'image(nwrite) & " freezes=" & integer'image(freezes) &
		       " frozen_cycles=" & integer'image(frozen_cycles) severity note;
		done <= true;
		wait;
	end process;
end sim;
