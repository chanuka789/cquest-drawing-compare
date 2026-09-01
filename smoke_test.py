import webview

webview.create_window(
    "C-Quest Drawing Compare",
    html="""
    <body style="margin:0;background:#14181C;color:#EDF1F5;
                 font-family:system-ui;display:grid;place-items:center;height:100vh">
      <div style="text-align:center">
        <div style="width:3px;height:80px;background:#CF0A2C;margin:0 auto 24px;
                    box-shadow:0 0 24px rgba(207,10,44,.5)"></div>
        <h1 style="font-weight:600;margin:0">Environment is working</h1>
        <p style="color:#9DA9B5;margin-top:8px">WebView2 is rendering correctly.</p>
      </div>
    </body>
    """,
    width=900,
    height=600,
)
webview.start()
