// ==UserScript==
// @name         美客多验证码 gstatic.cn 改写
// @namespace    seller-auto
// @version      1.0.0
// @description  美客多验证页 CSP 只放行 gstatic.com。enterprise.js 若要插入 www.gstatic.cn，先改写成 www.gstatic.com，避免 reCAPTCHA 一直转圈。
// @author       seller-auto
// @match        *://*.mercadolibre.com/*
// @match        *://mercadolibre.com/*
// @match        *://*.mercadolibre.com.ar/*
// @match        *://mercadolibre.com.ar/*
// @match        *://*.mercadolibre.com.bo/*
// @match        *://mercadolibre.com.bo/*
// @match        *://*.mercadolibre.com.br/*
// @match        *://mercadolibre.com.br/*
// @match        *://*.mercadolibre.cl/*
// @match        *://mercadolibre.cl/*
// @match        *://*.mercadolibre.com.co/*
// @match        *://mercadolibre.com.co/*
// @match        *://*.mercadolibre.co.cr/*
// @match        *://mercadolibre.co.cr/*
// @match        *://*.mercadolibre.com.do/*
// @match        *://mercadolibre.com.do/*
// @match        *://*.mercadolibre.com.ec/*
// @match        *://mercadolibre.com.ec/*
// @match        *://*.mercadolibre.com.gt/*
// @match        *://mercadolibre.com.gt/*
// @match        *://*.mercadolibre.com.hn/*
// @match        *://mercadolibre.com.hn/*
// @match        *://*.mercadolibre.com.mx/*
// @match        *://mercadolibre.com.mx/*
// @match        *://*.mercadolibre.com.ni/*
// @match        *://mercadolibre.com.ni/*
// @match        *://*.mercadolibre.com.pa/*
// @match        *://mercadolibre.com.pa/*
// @match        *://*.mercadolibre.com.py/*
// @match        *://mercadolibre.com.py/*
// @match        *://*.mercadolibre.com.pe/*
// @match        *://mercadolibre.com.pe/*
// @match        *://*.mercadolibre.com.sv/*
// @match        *://mercadolibre.com.sv/*
// @match        *://*.mercadolibre.com.uy/*
// @match        *://mercadolibre.com.uy/*
// @match        *://*.mercadolibre.com.ve/*
// @match        *://mercadolibre.com.ve/*
// @match        *://*.mercadolivre.com/*
// @match        *://mercadolivre.com/*
// @match        *://*.mercadolivre.com.br/*
// @match        *://mercadolivre.com.br/*
// @match        *://*.mercadopago.com/*
// @match        *://mercadopago.com/*
// @match        *://*.mercadopago.com.ar/*
// @match        *://mercadopago.com.ar/*
// @match        *://*.mercadopago.com.mx/*
// @match        *://mercadopago.com.mx/*
// @match        *://*.mercadopago.com.br/*
// @match        *://mercadopago.com.br/*
// @match        *://*.mercadopago.cl/*
// @match        *://mercadopago.cl/*
// @match        *://*.mercadopago.com.co/*
// @match        *://mercadopago.com.co/*
// @match        *://*.mercadopago.com.pe/*
// @match        *://mercadopago.com.pe/*
// @match        *://*.mercadopago.com.uy/*
// @match        *://mercadopago.com.uy/*
// @run-at       document-start
// @inject-into  page
// @grant        none
// ==/UserScript==

/*
 * 与 seller_auto/browser_launch.py 里的 RECAPTCHA_CDN_FIX_JS 相同：
 * 在插入脚本前改 src，不替换已经失败的请求。
 * 不要加 @noframes，同源 iframe 里的 enterprise.js 也要改写。
 */
(function () {
  "use strict";

  const TOKEN = "__mlGstaticFix";

  function rewrite(value) {
    const text = String(value || "");
    const fixed = text.replace(new RegExp("//(?:www\\.)?gstatic\\.cn/", "gi"), "//www.gstatic.com/");
    if (fixed === text) return text;
    if (text.length <= 300) {
      console.info("[美客多验证码] gstatic.cn → gstatic.com:", text, "=>", fixed);
    } else {
      console.info("[美客多验证码] 已把插入内容中的 gstatic.cn 改写为 gstatic.com");
    }
    return fixed;
  }

  function isScriptSrc(element, name) {
    return !!element && element.localName === "script" && String(name).toLowerCase() === "src";
  }

  try {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, "src");
    if (descriptor && descriptor.set && descriptor.get && !descriptor.set[TOKEN]) {
      function set(value) {
        return descriptor.set.call(this, rewrite(value));
      }
      set[TOKEN] = true;
      Object.defineProperty(HTMLScriptElement.prototype, "src", {
        configurable: true,
        enumerable: descriptor.enumerable,
        get() {
          return descriptor.get.call(this);
        },
        set,
      });
    }
  } catch (error) {
    console.warn("[美客多验证码] 未能改写 script.src", error);
  }

  try {
    const setAttribute = Element.prototype.setAttribute;
    if (setAttribute && !setAttribute[TOKEN]) {
      function setAttributeFixed(name, value) {
        if (isScriptSrc(this, name)) return setAttribute.call(this, name, rewrite(value));
        return setAttribute.call(this, name, value);
      }
      setAttributeFixed[TOKEN] = true;
      Element.prototype.setAttribute = setAttributeFixed;
    }
  } catch (error) {
    console.warn("[美客多验证码] 未能改写 setAttribute", error);
  }

  try {
    const setAttributeNS = Element.prototype.setAttributeNS;
    if (setAttributeNS && !setAttributeNS[TOKEN]) {
      function setAttributeNSFixed(namespace, name, value) {
        if (isScriptSrc(this, name)) return setAttributeNS.call(this, namespace, name, rewrite(value));
        return setAttributeNS.call(this, namespace, name, value);
      }
      setAttributeNSFixed[TOKEN] = true;
      Element.prototype.setAttributeNS = setAttributeNSFixed;
    }
  } catch (error) {
    console.warn("[美客多验证码] 未能改写 setAttributeNS", error);
  }

  function wrapWrite(original) {
    if (!original || original[TOKEN]) return original;
    function writeFixed(...args) {
      for (let index = 0; index < args.length; index += 1) {
        if (typeof args[index] === "string") args[index] = rewrite(args[index]);
      }
      return original.apply(this, args);
    }
    writeFixed[TOKEN] = true;
    return writeFixed;
  }

  try {
    Document.prototype.write = wrapWrite(Document.prototype.write);
    Document.prototype.writeln = wrapWrite(Document.prototype.writeln);
  } catch (error) {
    console.warn("[美客多验证码] 未能改写 document.write", error);
  }
})();
