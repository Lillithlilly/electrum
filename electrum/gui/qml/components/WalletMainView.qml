import QtQuick 2.6
import QtQuick.Controls 2.3
import QtQuick.Layouts 1.0
import QtQml 2.6

Item {
    id: rootItem

    property string title: Daemon.walletName

    property QtObject menu: Menu {
        MenuItem { text: 'Wallets'; onTriggered: stack.push(Qt.resolvedUrl('Wallets.qml')) }
        MenuItem { text: 'Network'; onTriggered: stack.push(Qt.resolvedUrl('NetworkStats.qml')) }
    }

    ColumnLayout {
        anchors.fill: parent

        TabBar {
            id: tabbar
            Layout.fillWidth: true
            currentIndex: swipeview.currentIndex
            TabButton {
                text: qsTr('Receive')
            }
            TabButton {
                text: qsTr('History')
            }
            TabButton {
                enabled: !Daemon.currentWallet.isWatchOnly
                text: qsTr('Send')
            }
        }

        SwipeView {
            id: swipeview

            Layout.fillHeight: true
            Layout.fillWidth: true
            currentIndex: tabbar.currentIndex

            Item {

                ColumnLayout {
                    width: parent.width
                    y: 20
                    spacing: 20
                }
            }

            Item {
                History {
                    id: history
                    anchors.fill: parent
                }
            }


            Item {
                enabled: !Daemon.currentWallet.isWatchOnly
                Send {
                    anchors.fill: parent
                }
            }

        }

    }

}

