(function (global) {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  $.ajaxSetup({
    beforeSend: function (xhr, settings) {
      if (!/^(GET|HEAD|OPTIONS|TRACE)$/i.test(settings.type) && csrfToken) {
        xhr.setRequestHeader("X-CSRF-Token", csrfToken);
      }
    }
  });

  function formatDenarii(atomicValue, includeNotation) {
    var atomic = BigInt(String(atomicValue || "0"));
    var units = 100000000n;
    var whole = atomic / units;
    var fractional = (atomic % units).toString().padStart(8, "0").replace(/0+$/, "");
    var value = fractional ? whole.toString() + "." + fractional : whole.toString();
    return includeNotation === false ? value : value + " DEN";
  }

  function formatDate(timestamp) {
    return new Date(Number(timestamp) * 1000).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function setNodeStatus(online) {
    var indicator = document.getElementById("node_indicator");
    var text = document.getElementById("node_status_text");
    if (!indicator || !text) {
      return;
    }
    indicator.classList.toggle("online", online);
    indicator.classList.toggle("offline", !online);
    text.textContent = online ? "Node online" : "Node unavailable";
  }

  function notice(message, type) {
    var box = document.getElementById("console_notice");
    if (!box) {
      return;
    }
    box.className = "alert console-notice alert-" + (type || "info");
    box.textContent = message;
    box.style.display = "block";
    global.setTimeout(function () {
      box.style.display = "none";
    }, 5000);
  }

  global.DenariusConsole = {
    formatDenarii: formatDenarii,
    formatDate: formatDate,
    notice: notice,
    setNodeStatus: setNodeStatus
  };

  $.getJSON("/api/chain")
    .done(function () { setNodeStatus(true); })
    .fail(function () { setNodeStatus(false); });
})(window);
