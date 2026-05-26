package com.claudebridge.app

import android.content.SharedPreferences
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

class SettingsActivity : AppCompatActivity() {

    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE)

        val input = findViewById<EditText>(R.id.input_url)
        val saveBtn = findViewById<Button>(R.id.btn_save)
        val hint = findViewById<TextView>(R.id.txt_hint)

        input.setText(prefs.getString(MainActivity.KEY_DASHBOARD_URL, MainActivity.DEFAULT_URL))

        hint.text = getString(R.string.settings_hint)

        saveBtn.setOnClickListener {
            var url = input.text.toString().trim()
            if (url.isBlank()) {
                Toast.makeText(this, R.string.toast_url_empty, Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                url = "http://$url"
            }
            // 去掉尾部斜杠
            url = url.trimEnd('/')
            prefs.edit().putString(MainActivity.KEY_DASHBOARD_URL, url).apply()
            Toast.makeText(this, R.string.toast_saved, Toast.LENGTH_SHORT).show()
            finish()
        }

        // 快捷填入按钮
        findViewById<Button>(R.id.btn_lan).setOnClickListener {
            input.setText("http://192.168.1.100:8888")
            input.setSelection(input.text.length)
        }
        findViewById<Button>(R.id.btn_ngrok).setOnClickListener {
            input.setText("https://your-ngrok-id.ngrok-free.app")
            input.setSelection(input.text.length)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
