(function(window) {
	function trimTrailingSlash(value) {
		return String(value || '').replace(/\/+$/, '');
	}

	function currentClusterFromQuery() {
		var match = (window.location.search || '').match(/(?:\?|&)cluster=([^&]+)/);
		return match ? decodeURIComponent(match[1]) : '';
	}

	function appendCluster(path, cluster) {
		if (!cluster || /(?:\?|&)cluster=/.test(path)) {
			return path;
		}

		return path + (path.indexOf('?') === -1 ? '?' : '&') + 'cluster=' + encodeURIComponent(cluster);
	}

	window.squidvizGetCluster = function() {
		return String(window.SQUIDVIZ_CLUSTER || currentClusterFromQuery() || '');
	};

	window.squidvizSetCluster = function(cluster) {
		window.SQUIDVIZ_CLUSTER = String(cluster || '');
	};

	window.squidvizDataUrl = function(path) {
		var normalizedPath = path.charAt(0) === '/' ? path : '/' + path;
		var cluster = window.squidvizGetCluster();
		var backendUrl = trimTrailingSlash(window.SQUIDVIZ_BACKEND_URL);
		return backendUrl + appendCluster(normalizedPath, cluster);
	};
})(window);
