<?php

require_once __DIR__ . '/common.php';

$input_json = ceph_json_command(array("-s", "-f", "json"));
$pgmap = value_or($input_json->pgmap, (object) array());
$include_latency = isset($_GET["latency"]) && $_GET["latency"] === "1";

$iops_read = (float) value_or($pgmap->read_op_per_sec, 0);
$iops_write = (float) value_or($pgmap->write_op_per_sec, 0);
$bytes_read = (float) value_or($pgmap->read_bytes_sec, 0);
$bytes_write = (float) value_or($pgmap->write_bytes_sec, 0);
$commit_latency_ms = null;
$apply_latency_ms = null;

$osd_perf = $include_latency ? ceph_json_command_optional(array("osd", "perf", "--format=json")) : null;
if ($osd_perf !== null) {
	$perf_entries = array();

	if (is_array($osd_perf)) {
		$perf_entries = $osd_perf;
	} elseif (isset($osd_perf->osdstats->osd_perf_infos) && is_array($osd_perf->osdstats->osd_perf_infos)) {
		$perf_entries = $osd_perf->osdstats->osd_perf_infos;
	} elseif (isset($osd_perf->osd_perf_infos) && is_array($osd_perf->osd_perf_infos)) {
		$perf_entries = $osd_perf->osd_perf_infos;
	}

	$commit_sum = 0.0;
	$apply_sum = 0.0;
	$count = 0;

	foreach ($perf_entries as $entry) {
		$commit_value = null;
		$apply_value = null;

		if (isset($entry->commit_latency_ms)) {
			$commit_value = (float) $entry->commit_latency_ms;
		} elseif (isset($entry->perf_stat->commit_latency_ms)) {
			$commit_value = (float) $entry->perf_stat->commit_latency_ms;
		} elseif (isset($entry->perf_stats->commit_latency_ms)) {
			$commit_value = (float) $entry->perf_stats->commit_latency_ms;
		}

		if (isset($entry->apply_latency_ms)) {
			$apply_value = (float) $entry->apply_latency_ms;
		} elseif (isset($entry->perf_stat->apply_latency_ms)) {
			$apply_value = (float) $entry->perf_stat->apply_latency_ms;
		} elseif (isset($entry->perf_stats->apply_latency_ms)) {
			$apply_value = (float) $entry->perf_stats->apply_latency_ms;
		}

		if ($commit_value !== null || $apply_value !== null) {
			$commit_sum += $commit_value !== null ? $commit_value : 0.0;
			$apply_sum += $apply_value !== null ? $apply_value : 0.0;
			$count++;
		}
	}

	if ($count > 0) {
		$commit_latency_ms = $commit_sum / $count;
		$apply_latency_ms = $apply_sum / $count;
	}
}

respond_json(array(
	"ok" => true,
	"bytes_rd" => $bytes_read,
	"bytes_wr" => $bytes_write,
	"ops_read" => $iops_read,
	"ops_write" => $iops_write,
	"ops" => $iops_read + $iops_write,
	"latency_enabled" => $include_latency,
	"commit_latency_ms" => $commit_latency_ms,
	"apply_latency_ms" => $apply_latency_ms,
));
