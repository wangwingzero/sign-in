function qaxInject() {
    this.redirectUrl = chrome.runtime.getURL("page/security/warn.html?refer=" + window.location.href);
}

qaxInject.prototype.start = function () {
    var s = this;

    setTimeout(function() {
        chrome.runtime.sendMessage({type: "checkRisk", url: window.location.href }, function(e) {
			console.log('category=%d', e.category);
			if (e && ((e.category == 33) || (e.category == 34) || (e.category == 48) || (e.category == 49))) {
				return;
			}
			
			if (e && e.is_safe != 1) {
                    window.location.href = s.redirectUrl;
            }
            
        });
      }, 750);

    
}

let qaxInjectObj = new qaxInject;
qaxInjectObj.start();
