# TimeQuest report for the dashboard revision (Quartus 17.0.2 Standard).
#   quartus_sta -t scripts/report_dashboard_timing.tcl MegaCD MegaCD_Dashboard
#
# Writes output_files/<rev>.dashboard_timing.rpt with check_timing, clocks,
# unconstrained paths, clock transfers, and per-operating-condition
# setup/hold/recovery/removal summaries. Exits 1 if any analysed operating
# condition has negative worst slack. The QSF disables automatic multicorner
# analysis, so every available operating condition is analysed here explicitly.
#
# Status: written against the documented quartus::sta command set; not yet run
# on a build host. Verify its output against the TimeQuest GUI on first use.

package require ::quartus::project
package require ::quartus::sta

set project  [lindex $quartus(args) 0]
set revision [lindex $quartus(args) 1]
if {$project eq "" || $revision eq ""} {
	post_message -type error "usage: quartus_sta -t report_dashboard_timing.tcl <project> <revision>"
	qexit -error
}

project_open $project -revision $revision
create_timing_netlist
read_sdc
update_timing_netlist

set rpt "output_files/${revision}.dashboard_timing.rpt"
file delete -force $rpt

check_timing -file $rpt -append
report_clocks -file $rpt -append
report_ucp -file $rpt -append
report_clock_transfers -file $rpt -append

set failed 0
foreach_in_collection op [get_available_operating_conditions] {
	set_operating_conditions $op
	update_timing_netlist
	set opname [get_operating_conditions_info -display_name $op]
	foreach kind {setup hold recovery removal} {
		set res [report_timing -$kind -npaths 10 -detail summary -file $rpt -append \
			-panel_name "$opname $kind"]
		set npaths [lindex $res 0]
		set slack  [lindex $res 1]
		post_message "$opname $kind: worst slack $slack ($npaths paths)"
		if {$npaths > 0 && $slack < 0} {
			post_message -type error "$opname $kind: NEGATIVE slack $slack"
			set failed 1
		}
	}
	create_timing_summary -setup -file $rpt -append
	create_timing_summary -hold -file $rpt -append
}

delete_timing_netlist
project_close

if {$failed} { qexit -error }
qexit -success
