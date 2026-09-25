import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "jeffe747.cisco-vpn"

  property string server: ""
  property string username: ""
  property bool serverDirty: false
  property bool usernameDirty: false
  property string errorText: ""
  property bool connected: false
  property bool busy: false
  property bool cancelRequested: false
  property string action: ""
  property string request: ""
  property bool dependenciesKnown: false
  property var missingPackages: []
  readonly property string helper: Qt.resolvedUrl("backend.py").toString().replace(/^file:\/\//, "")
  readonly property string dependencyHelper: Qt.resolvedUrl("dependencies.py").toString().replace(/^file:\/\//, "")
  readonly property bool dependenciesReady: dependenciesKnown && missingPackages.length === 0

  implicitWidth: icon.implicitWidth
  implicitHeight: icon.implicitHeight

  function refresh() {
    if (busy || dependencyProcess.running) return
    if (!dependenciesKnown) {
      dependencyProcess.running = true
      return
    }
    if (!dependenciesReady || statusProcess.running) return
    statusProcess.running = true
  }

  function installDependencies() {
    if (busy || dependenciesReady || !dependenciesKnown) return
    busy = true
    action = "install"
    errorText = ""
    installProcess.running = true
  }

  function applyResult(text, exitCode, showError) {
    try {
      var data = JSON.parse(text)
      if (!data.ok || exitCode !== 0) {
        if (showError) errorText = data.error || "VPN operation failed"
        return
      }
      connected = data.connected === true
      if (showError && data.connected === true) {
        serverDirty = false
        usernameDirty = false
        errorText = ""
      }
      if (!opened) {
        if (!serverDirty) server = data.server || ""
        if (!usernameDirty) username = data.username || ""
      }
    } catch (error) {
      if (showError) errorText = "VPN helper returned an invalid response"
    }
  }

  function connectVpn() {
    if (busy) return
    if (!serverField.text.trim() || !userField.text.trim() || !passwordField.text) {
      errorText = "Enter a server, username, and password"
      return
    }
    request = JSON.stringify({
      server: serverField.text.trim(),
      username: userField.text.trim(),
      password: passwordField.text
    })
    busy = true
    cancelRequested = false
    action = "connect"
    errorText = ""
    actionProcess.command = ["python", helper, "connect"]
    actionProcess.running = true
    passwordField.text = ""
  }

  function disconnectVpn() {
    if (busy) return
    busy = true
    action = "disconnect"
    errorText = ""
    actionProcess.command = ["python", helper, "disconnect"]
    actionProcess.running = true
  }

  function cancelVpn() {
    if (!busy || action !== "connect" || cancelRequested) return
    cancelRequested = true
    request = ""
    if (actionProcess.processId) actionProcess.signal(15)
  }

  onOpenedChanged: if (opened) {
    dependenciesKnown = false
    refresh()
  } else {
    passwordField.text = ""
  }

  Timer {
    interval: 5000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: dependencyProcess
    command: ["python", root.dependencyHelper]
    stdout: StdioCollector {
      id: dependencyOutput
      waitForEnd: true
    }
    onExited: function(code) {
      try {
        var result = JSON.parse(dependencyOutput.text)
        if (code !== 0 || result.ok !== true || !Array.isArray(result.missing))
          throw new Error("Invalid dependency check")
        root.missingPackages = result.missing
        root.dependenciesKnown = true
        if (root.dependenciesReady) {
          root.errorText = ""
          root.refresh()
          if (root.opened) Qt.callLater(function() { serverField.forceActiveFocus() })
        }
      } catch (error) {
        root.errorText = "Could not check installed packages; run omarchy pkg add networkmanager-openconnect python-gobject in a terminal"
      }
    }
  }

  Process {
    id: installProcess
    command: ["pkexec", "/usr/share/omarchy/bin/omarchy-pkg-add",
              "networkmanager-openconnect", "python-gobject"]
    onExited: function(code) {
      root.busy = false
      root.dependenciesKnown = false
      if (code !== 0)
        root.errorText = "Installation failed or was cancelled; try again or run omarchy pkg add networkmanager-openconnect python-gobject in a terminal"
      root.refresh()
    }
  }

  Process {
    id: statusProcess
    command: ["python", root.helper, "status"]
    stdout: StdioCollector {
      id: statusOutput
      waitForEnd: true
    }
    onExited: function(code) {
      if (code === 0) root.applyResult(statusOutput.text, code, false)
      else if (root.opened && root.errorText === "") root.errorText = "Could not read VPN status"
    }
  }

  Process {
    id: actionProcess
    stdinEnabled: true
    onStarted: {
      if (root.cancelRequested) {
        signal(15)
      } else {
        if (root.request !== "") write(root.request + "\n")
        root.request = ""
      }
    }
    stdout: StdioCollector {
      id: actionOutput
      waitForEnd: true
    }
    onExited: function(code) {
      root.busy = false
      if (root.cancelRequested) root.errorText = "Connection cancelled"
      else root.applyResult(actionOutput.text, code, true)
      root.cancelRequested = false
      root.refresh()
    }
  }

  BarIconButton {
    id: icon
    anchors.fill: parent
    bar: root.bar
    text: root.connected ? "󰌆" : "󰦝"
    tooltipText: root.connected ? "Cisco VPN connected" : "Cisco VPN disconnected"
    onPressed: root.toggle()
  }

  KeyboardPanel {
    anchorItem: icon
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: serverField
    contentWidth: fittedContentWidth(Style.space(330))
    contentHeight: fittedContentHeight(fields.implicitHeight)

    Column {
      id: fields
      width: parent.width
      spacing: Style.space(8)

      Text {
        text: root.busy ? (root.action === "connect" ? "Cisco VPN · Connecting…" : (root.action === "install" ? "Cisco VPN · Installing packages…" : "Cisco VPN · Disconnecting…")) : (root.connected ? "Cisco VPN · Connected" : "Cisco VPN · Disconnected")
        color: root.barForeground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
      }

      Text {
        visible: root.dependenciesReady
        width: parent.width
        wrapMode: Text.WordWrap
        text: "Connect updates NetworkManager profile “Omarchy Cisco VPN” (server and username)"
        color: root.barForeground
        opacity: 0.65
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        visible: !root.dependenciesReady && root.dependenciesKnown
        width: parent.width
        wrapMode: Text.WordWrap
        text: "This widget needs " + root.missingPackages.join(" and ") + ". Install the missing packages? Administrator authentication is required."
        textFormat: Text.PlainText
        color: root.barForeground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
      }

      TextField {
        id: serverField
        visible: root.dependenciesReady
        width: parent.width
        placeholderText: "Server (vpn.example.com)"
        text: root.server
        enabled: !root.busy && !root.connected
        onTextChanged: {
          if (root.opened) root.serverDirty = true
          root.server = text
        }
        onAccepted: userField.forceActiveFocus()
      }

      TextField {
        id: userField
        visible: root.dependenciesReady
        width: parent.width
        placeholderText: "Username"
        text: root.username
        enabled: !root.busy && !root.connected
        onTextChanged: {
          if (root.opened) root.usernameDirty = true
          root.username = text
        }
        onAccepted: passwordField.forceActiveFocus()
      }

      TextField {
        id: passwordField
        visible: root.dependenciesReady
        width: parent.width
        placeholderText: "Password"
        password: true
        enabled: !root.busy && !root.connected
        onAccepted: root.connectVpn()
      }

      Text {
        visible: root.errorText !== ""
        width: parent.width
        wrapMode: Text.WordWrap
        textFormat: Text.PlainText
        text: root.errorText
        color: root.bar ? root.bar.urgent : Color.urgent
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Rectangle {
        visible: root.dependenciesReady || root.dependenciesKnown
        width: parent.width
        height: Style.space(36)
        radius: Style.cornerRadius
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: root.busy ? 0.5 : 1

        Text {
          anchors.centerIn: parent
          text: root.busy ? (root.action === "connect" ? (root.cancelRequested ? "Cancelling…" : "Cancel") : "Working…") : (!root.dependenciesReady ? "Install packages" : (root.connected ? "Disconnect" : "Connect"))
          color: Color.background
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
        }
        MouseArea {
          anchors.fill: parent
          enabled: (!root.busy && root.dependenciesKnown) || (root.action === "connect" && !root.cancelRequested)
          cursorShape: Qt.PointingHandCursor
          onClicked: root.busy ? root.cancelVpn() : (!root.dependenciesReady ? root.installDependencies() : (root.connected ? root.disconnectVpn() : root.connectVpn()))
        }
      }
    }
  }
}
