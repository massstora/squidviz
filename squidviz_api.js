(function(window) {
	function trimTrailingSlash(value) {
		return String(value || '').replace(/\/+$/, '');
	}

	window.squidvizDataUrl = function(path) {
		var normalizedPath = path.charAt(0) === '/' ? path : '/' + path;
		var backendUrl = trimTrailingSlash(window.SQUIDVIZ_BACKEND_URL);
		return backendUrl + normalizedPath;
	};
})(window);
