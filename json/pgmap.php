<?php

require_once __DIR__ . '/common.php';

$status = ceph_json_command(array("-s", "-f", "json"));

$pgmap = value_or($status->pgmap, (object) array());
$version = value_or($pgmap->version, null);
$pgs_by_state = value_or($pgmap->pgs_by_state, array());
$unhealthy_pgs = 0;

if (is_array($pgs_by_state)) {
	foreach ($pgs_by_state as $state_entry) {
		$state_name = value_or($state_entry->state_name, "");
		$count = (int) value_or($state_entry->count, 0);

		if ($state_name !== "active+clean") {
			$unhealthy_pgs += $count;
		}
	}
}

if ($version === null) {
	$summary = array(
		"num_pgs" => value_or($pgmap->num_pgs, 0),
		"read_op_per_sec" => value_or($pgmap->read_op_per_sec, 0),
		"write_op_per_sec" => value_or($pgmap->write_op_per_sec, 0),
		"recovering_objects_per_sec" => value_or($pgmap->recovering_objects_per_sec, 0),
		"pgs_by_state" => $pgs_by_state,
	);

	$version = sha1(json_encode($summary));
}

respond_json(array(
	"ok" => true,
	"pgmap" => (string) $version,
	"unhealthy_pgs" => $unhealthy_pgs,
	"has_unhealthy" => $unhealthy_pgs > 0,
));
