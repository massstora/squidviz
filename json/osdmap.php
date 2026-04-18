<?php

require_once __DIR__ . '/common.php';

$tree = ceph_json_command(array("osd", "tree", "--format=json"));

$nodes = value_or($tree->nodes, array());
$summary = array();

if (is_array($nodes) || is_object($nodes)) {
	foreach ($nodes as $node) {
		$summary[] = array(
			"id" => value_or($node->id, null),
			"name" => value_or($node->name, ""),
			"type" => value_or($node->type, ""),
			"status" => value_or($node->status, ""),
			"reweight" => value_or($node->reweight, null),
			"children" => value_or($node->children, array()),
		);
	}
}

$version = sha1(json_encode($summary));

respond_json(array(
	"ok" => true,
	"osdmap" => (string) $version,
));
