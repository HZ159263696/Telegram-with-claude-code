package com.claudebridge.app

import android.annotation.SuppressLint
import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.os.Bundle
import android.view.KeyEvent
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var prefs: SharedPreferences

    companion object {
        const val PREFS_NAME = "claude_bridge_prefs"
        const val KEY_DASHBOARD_URL = "dashboard_url"
        const val DEFAULT_URL = ""
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        webView = findViewById(R.id.webview)
        progressBar = findViewById(R.id.progress)
        swipe = findViewById(R.id.swipe)

        setupWebView()
        swipe.setOnRefreshListener { webView.reload() }

        val url = prefs.getString(KEY_DASHBOARD_URL, DEFAULT_URL) ?: ""
        if (url.isBlank()) {
            // 首次启动，跳设置页
            startActivity(Intent(this, SettingsActivity::class.java))
            Toast.makeText(this, R.string.toast_set_url_first, Toast.LENGTH_LONG).show()
        } else {
            webView.loadUrl(url)
        }
    }

    override fun onResume() {
        super.onResume()
        // 从设置页回来时，如果 URL 变了就重载
        val url = prefs.getString(KEY_DASHBOARD_URL, DEFAULT_URL) ?: ""
        val currentUrl = webView.url ?: ""
        if (url.isNotBlank() && !currentUrl.startsWith(url)) {
            webView.loadUrl(url)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val s: WebSettings = webView.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.databaseEnabled = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.setSupportZoom(true)
        s.builtInZoomControls = true
        s.displayZoomControls = false
        s.cacheMode = WebSettings.LOAD_DEFAULT
        s.mediaPlaybackRequiresUserGesture = false
        s.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        s.userAgentString = s.userAgentString + " ClaudeBridgeApp/1.0"

        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                progressBar.visibility = View.VISIBLE
            }
            override fun onPageFinished(view: WebView, url: String) {
                progressBar.visibility = View.GONE
                swipe.isRefreshing = false
            }
            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                // ngrok / 自签证书场景：允许通过
                handler?.proceed()
            }
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                // 外链 (mailto/tel/telegram) 交给系统
                if (url.startsWith("http://") || url.startsWith("https://")) {
                    return false
                }
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                } catch (_: Exception) {}
                return true
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                progressBar.progress = newProgress
                if (newProgress >= 100) progressBar.visibility = View.GONE
            }
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            R.id.action_reload -> {
                webView.reload()
                true
            }
            R.id.action_home -> {
                val url = prefs.getString(KEY_DASHBOARD_URL, DEFAULT_URL) ?: ""
                if (url.isNotBlank()) webView.loadUrl(url)
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}
