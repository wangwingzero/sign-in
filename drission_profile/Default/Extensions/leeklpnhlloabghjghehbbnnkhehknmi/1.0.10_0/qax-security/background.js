
var wh = {};

function myBrowser() {
	var userAgent = navigator.userAgent; //È¡µÃä¯ÀÀÆ÷µÄuserAgent×Ö·û´®
	var isOpera = userAgent.indexOf("Opera") > -1; //ÅÐ¶ÏÊÇ·ñOperaä¯ÀÀÆ÷
	var isEdge = userAgent.indexOf("Edg") > -1; //ÅÐ¶ÏÊÇ·ñIEµÄEdgeä¯ÀÀÆ÷
	var isFF = userAgent.indexOf("Firefox") > -1; //ÅÐ¶ÏÊÇ·ñFirefoxä¯ÀÀÆ÷
	var isSafari = userAgent.indexOf("Safari") > -1
		&& userAgent.indexOf("Chrome") == -1; //ÅÐ¶ÏÊÇ·ñSafariä¯ÀÀÆ÷
	var isChrome = userAgent.indexOf("Chrome") > -1
		&& userAgent.indexOf("Safari") > -1; //ÅÐ¶ÏChromeä¯ÀÀÆ÷

	if (isOpera) {
		return "opera";
	}
	if (isEdge) {
		return "edge";
	}
	if (isSafari) {
		return "safari";
	}
	if (isChrome) {
		return "chrome";
	}

	return "";
}

chrome.runtime.onMessage.addListener(function (request,sender,callback) {
    let  tabId = sender.tab.id;
    var arr = wh[tabId];
    switch (request.type) {
      case 'close.currrent.tab':
        chrome.runtime.sendNativeMessage( "com.qax.qax_security_host",{
          type: "url_close",
          url: "",
          src: myBrowser()
      },function(navMsg){
      });
        chrome.tabs.remove(tabId);
        return;
      case 'explore':
        let url = request.host;
        let host = url.split("?")[0];
        if (!arr) {
          arr = [];
        }
        if (arr.indexOf(host) < 0) {
          arr.push(host);
        }

        wh[tabId] = arr;
        chrome.runtime.sendNativeMessage( "com.qax.qax_security_host",{
          type: "url_passthrough",
          url: "",
          src: myBrowser()
      },function(navMsg){
        
      });
      callback({"ret":"ok"});
        return;
      case 'checkRisk':  
        if(arr && arr.length > 0) {
          for(let i = 0; i < arr.length; i++) {
            if (request.url.includes(arr[i])) { 
                return true; 
            }
          }
        }

        chrome.runtime.sendNativeMessage( "com.qax.qax_security_host",{
            type: "url_check",
            url: request.url,
            src: myBrowser()
        }, function(navMsg) {
          if (typeof(navMsg) != 'undefined') {
            callback(navMsg);
          }
        });
        return true;
      default:
        return;
    }
});

chrome.tabs.onRemoved.addListener(function (tabId) {
  delete wh[tabId];
});
