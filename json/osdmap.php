<?php

require_once __DIR__ . '/common.php';

$status = ceph_json_command(array("-s", "-f", "json"));

$osdmap = value_or($status->osdmap, (object) array());
$version = value_or($osdmap->epoch, null);

if ($version === null) {
	$summary = array(
		"num_osds" => value_or($osdmap->num_osds, 0),
		"num_up_osds" => value_or($osdmap->num_up_osds, 0),
		"num_in_osds" => value_or($osdmap->num_in_osds, 0),
		"num_remapped_pgs" => value_or($osdmap->num_remapped_pgs, 0),
	);

	$version = sha1(json_encode($summary));
}

respond_json(array(
	"ok" => true,
	"osdmap" => (string) $version,
));
