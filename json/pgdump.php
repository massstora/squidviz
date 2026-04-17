<?php

require_once __DIR__ . '/common.php';

$pools = array();

$osd_dump = ceph_json_command(array("osd", "dump", "--format=json"));
if (!empty($osd_dump->pools)) {
	foreach ($osd_dump->pools as $pool) {
		$pools[$pool->pool] = $pool->pool_name;
	}
}

$pg_dump = ceph_json_command_fallback(array(
	array("pg", "dump_json", "--dumpcontents=pgs"),
	array("pg", "dump", "--format=json"),
));

$pg_stats = array();
if (isset($pg_dump->pg_stats) && is_array($pg_dump->pg_stats)) {
	$pg_stats = $pg_dump->pg_stats;
} elseif (isset($pg_dump->pg_map->pg_stats) && is_array($pg_dump->pg_map->pg_stats)) {
	$pg_stats = $pg_dump->pg_map->pg_stats;
}

$version = "unknown";
if (isset($pg_dump->version)) {
	$version = $pg_dump->version;
} elseif (isset($pg_dump->pg_map->version)) {
	$version = $pg_dump->pg_map->version;
}

$pg_tree = array(
	"name" => "cluster",
	"version" => $version,
	"children" => array(),
	"summary" => array(
		"total_pgs" => count($pg_stats),
		"problem_pgs" => 0,
	),
);

$pool_indexes = array();

foreach ($pg_stats as $pg) {
	$state = value_or($pg->state, "unknown");
	if ($state === "active+clean") {
		continue;
	}

	$pg_tree["summary"]["problem_pgs"]++;

	$pgid = value_or($pg->pgid, "");
	$parts = explode(".", $pgid, 2);
	$pool_id = $parts[0];
	$group = count($parts) > 1 ? $parts[1] : $pgid;

	$pool_name = isset($pools[$pool_id]) ? $pools[$pool_id] : "pool " . $pool_id;

	$pg_node = array(
		"name" => $group,
		"pool_name" => $pool_name,
		"pgid" => $pgid,
		"objects" => value_or(value_or($pg->stat_sum, (object) array())->num_objects, 0),
		"state" => $state,
	);

	if (!isset($pool_indexes[$pool_id])) {
		$pool_indexes[$pool_id] = count($pg_tree["children"]);
		$pg_tree["children"][] = array(
			"name" => $pool_id,
			"pool_name" => $pool_name,
			"children" => array(),
		);
	}

	$pool_index = $pool_indexes[$pool_id];
	$pg_tree["children"][$pool_index]["children"][] = $pg_node;
}

respond_json($pg_tree);
