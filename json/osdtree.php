<?php

require_once __DIR__ . '/common.php';

$input_json = ceph_json_command(array("osd", "tree", "--format=json"));

if (empty($input_json->nodes)) {
	fail_json("Ceph OSD tree did not contain any nodes.");
}

$nodes = array();
$root_id = null;

foreach ($input_json->nodes as $node) {
	$nodes[$node->id] = $node;

	if (isset($node->type) && $node->type === "root" && $root_id === null) {
		$root_id = $node->id;
	}
}

if ($root_id === null && isset($nodes[-1])) {
	$root_id = -1;
}

if ($root_id === null) {
	fail_json("Unable to determine the root of the Ceph OSD tree.");
}

function buildNode($node_id) {
	global $nodes;

	if (!isset($nodes[$node_id])) {
		return null;
	}

	$node = $nodes[$node_id];
	$children = array();

	if (!empty($node->children)) {
		foreach ($node->children as $child_id) {
			$child = buildNode($child_id);
			if ($child !== null) {
				$children[] = $child;
			}
		}
	}

	return array(
		"name" => value_or($node->name, (string) $node_id),
		"type" => value_or($node->type, "unknown"),
		"status" => value_or($node->status, "unknown"),
		"children" => $children,
	);
}

respond_json(buildNode($root_id));
