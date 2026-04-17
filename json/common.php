<?php

$squidviz_config = array(
	"ceph_bin" => getenv("SQUIDVIZ_CEPH_BIN") ?: "/usr/bin/ceph",
	"ceph_name" => getenv("SQUIDVIZ_CEPH_NAME") ?: null,
	"ceph_keyring" => getenv("SQUIDVIZ_CEPH_KEYRING") ?: null,
);

$config_path = __DIR__ . '/config.php';
if (file_exists($config_path)) {
	$file_config = require $config_path;
	if (is_array($file_config)) {
		$squidviz_config = array_merge($squidviz_config, $file_config);
	}
}

function respond_json($payload, $status_code = 200) {
	http_response_code($status_code);
	header("Content-Type: application/json");
	print json_encode($payload);
	exit;
}

function fail_json($message, $details = array(), $status_code = 500) {
	$payload = array(
		"ok" => false,
		"error" => $message,
	);

	if (!empty($details)) {
		$payload["details"] = $details;
	}

	respond_json($payload, $status_code);
}

function ceph_json_command($arguments) {
	$command = ceph_command_prefix($arguments);

	$descriptors = array(
		0 => array("pipe", "r"),
		1 => array("pipe", "w"),
		2 => array("pipe", "w"),
	);

	$process = proc_open($command, $descriptors, $pipes);
	if (!is_resource($process)) {
		fail_json("Unable to start ceph command.");
	}

	fclose($pipes[0]);
	$stdout = stream_get_contents($pipes[1]);
	fclose($pipes[1]);
	$stderr = stream_get_contents($pipes[2]);
	fclose($pipes[2]);

	$exit_code = proc_close($process);

	if ($exit_code !== 0) {
		fail_json(
			"Ceph command failed.",
			array(
				"command" => implode(" ", $command),
				"stderr" => trim($stderr),
				"exit_code" => $exit_code,
			),
			502
		);
	}

	$data = json_decode($stdout);
	if (json_last_error() !== JSON_ERROR_NONE) {
		fail_json(
			"Ceph returned invalid JSON.",
			array(
				"command" => implode(" ", $command),
				"json_error" => json_last_error_msg(),
			),
			502
		);
	}

	return $data;
}

function ceph_json_command_fallback($commands) {
	$last_error = null;

	foreach ($commands as $arguments) {
		$command = ceph_command_prefix($arguments);
		$descriptors = array(
			0 => array("pipe", "r"),
			1 => array("pipe", "w"),
			2 => array("pipe", "w"),
		);

		$process = @proc_open($command, $descriptors, $pipes);
		if (!is_resource($process)) {
			$last_error = array(
				"command" => implode(" ", $command),
				"error" => "Unable to start process",
			);
			continue;
		}

		fclose($pipes[0]);
		$stdout = stream_get_contents($pipes[1]);
		fclose($pipes[1]);
		$stderr = stream_get_contents($pipes[2]);
		fclose($pipes[2]);
		$exit_code = proc_close($process);

		if ($exit_code !== 0) {
			$last_error = array(
				"command" => implode(" ", $command),
				"stderr" => trim($stderr),
				"exit_code" => $exit_code,
			);
			continue;
		}

		$data = json_decode($stdout);
		if (json_last_error() !== JSON_ERROR_NONE) {
			$last_error = array(
				"command" => implode(" ", $command),
				"json_error" => json_last_error_msg(),
			);
			continue;
		}

		return $data;
	}

	fail_json("All Ceph command fallbacks failed.", array("last_error" => $last_error), 502);
}

function value_or($value, $fallback) {
	return isset($value) ? $value : $fallback;
}

function ceph_json_command_optional($arguments) {
	$command = ceph_command_prefix($arguments);
	$descriptors = array(
		0 => array("pipe", "r"),
		1 => array("pipe", "w"),
		2 => array("pipe", "w"),
	);

	$process = @proc_open($command, $descriptors, $pipes);
	if (!is_resource($process)) {
		return null;
	}

	fclose($pipes[0]);
	$stdout = stream_get_contents($pipes[1]);
	fclose($pipes[1]);
	fclose($pipes[2]);

	$exit_code = proc_close($process);
	if ($exit_code !== 0) {
		return null;
	}

	$data = json_decode($stdout);
	if (json_last_error() !== JSON_ERROR_NONE) {
		return null;
	}

	return $data;
}

function ceph_command_prefix($arguments) {
	global $squidviz_config;

	$command = array($squidviz_config["ceph_bin"]);

	if (!empty($squidviz_config["ceph_name"])) {
		$command[] = "--name";
		$command[] = $squidviz_config["ceph_name"];
	}

	if (!empty($squidviz_config["ceph_keyring"])) {
		$command[] = "--keyring";
		$command[] = $squidviz_config["ceph_keyring"];
	}

	return array_merge($command, $arguments);
}
