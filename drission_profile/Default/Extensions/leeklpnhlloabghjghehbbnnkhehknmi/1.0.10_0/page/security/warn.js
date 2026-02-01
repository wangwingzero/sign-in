
const ignoreButton = document.getElementById("ignoreButton")
ignoreButton.onclick = function() {
  if (document.referrer.length > 0) {
    chrome.runtime.sendMessage({
      type: "explore",
      host: document.referrer
    }, ()=>{
      window.location.href = document.referrer;
    });
  } else {
    let refer = getParams("refer");
    if (refer) {
      chrome.runtime.sendMessage({
        type: "explore",
        host: refer
      }, ()=> {
        window.location.href = refer;
      });
    }
  }
  
}

const closeButton =  document.getElementById("closeButton")
closeButton.onclick = function() {
  chrome.runtime.sendMessage({
      type: "close.currrent.tab"
  });
}

const reportButton =  document.getElementById("reportButton")
reportButton.onclick = function() {
  reportButton.innerHTML = "<img src=\"./Icon_16)Check_Bold_@2x-1.png\" style=\"width: 13px; height: 13px;\" /> <span style=\"color: black;\">已提交</span>"
}

const officalWebsiteButton =  document.getElementById("officalWebsiteButton")
officalWebsiteButton.onclick = function() {
  window.open("https://ts.qianxin.com/", "_blank")
}

function getParams(key) {
  var reg = new RegExp("(^|&)" + key + "=([^&]*)(&|$)");
  var r = window.location.search.substr(1).match(reg);
  if (r != null) {
      return unescape(r[2]);
  }
  return null;
};
