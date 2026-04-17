<?php

require_once __DIR__ . '/common.php';

$input_json = ceph_json_command(array("-s", "-f", "json"));
$pgmap = value_or($input_json->pgmap, (object) array());

$iops_read = (float) value_or($pgmap->read_op_per_sec, 0);
$iops_write = (float) value_or($pgmap->write_op_per_sec, 0);
$bytes_read = (float) value_or($pgmap->read_bytes_sec, 0);
$bytes_write = (float) value_or($pgmap->write_bytes_sec, 0);

respond_json(array(
	"ok" => true,
	"bytes_rd" => $bytes_read,
	"bytes_wr" => $bytes_write,
	"ops_read" => $iops_read,
	"ops_write" => $iops_write,
	"ops" => $iops_read + $iops_write,
));
