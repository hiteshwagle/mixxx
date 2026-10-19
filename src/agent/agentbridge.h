#pragma once

#include <QElapsedTimer>
#include <QFutureWatcher>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QPointer>
#include <QSet>
#include <QTimer>
#include <QUrl>
#include <array>
#include <atomic>
#include <deque>
#include <memory>
#include <optional>

#include "agent/agentplayer.h"

namespace mixxx {

struct AgentBridgeConfig {
    QUrl backendUrl;
    QByteArray token;
    static std::optional<AgentBridgeConfig> fromFile(const QString& path, QString* pError);
};

class AgentBridge final : public QObject {
    Q_OBJECT
  public:
    AgentBridge(AgentBridgeConfig config,
            std::unique_ptr<AgentPlayer> player,
            QNetworkAccessManager* pNetwork = nullptr);
    ~AgentBridge() override;
    void start();
    void stop();

  private slots:
    void tick();
    void replyFinished();

  private:
    enum class Request { Register, State, Claim, Ack };
    enum class Execution { Idle, Verifying, Loading, Settling, Transition };
    struct Identity {
        QString path;
        QString id;
        qint64 size = 0;
        qint64 modified = 0;
    };
    struct HashResult {
        Identity identity;
        bool valid = false;
    };
    struct Ack {
        QString commandId;
        QJsonObject body;
    };

    void post(Request type, const QString& path, const QJsonObject& body);
    QJsonObject observedState() const;
    QJsonObject snapshot();
    void execute(const QJsonObject& command);
    void advance();
    void finish(bool success, const QString& error = QString());
    void resetConnection();
    void cancelExecution();
    bool matches(const QJsonObject& payload, const QJsonObject& state) const;
    QString validate(const QJsonObject& payload) const;
    void verifyLoad(const QJsonObject& payload);

    const AgentBridgeConfig m_config;
    std::unique_ptr<AgentPlayer> m_player;
    QNetworkAccessManager m_network;
    QNetworkAccessManager* const m_pNetwork;
    QPointer<QNetworkReply> m_pReply;
    QByteArray m_response;
    Request m_request = Request::Register;
    QTimer m_tickTimer;
    QTimer m_requestTimeout;
    QElapsedTimer m_clock;
    qint64 m_nextRequest = 0;
    qint64 m_nextState = 0;
    QString m_instance;
    qint64 m_sequence = 0;
    QSet<QString> m_seen;
    std::array<Identity, 2> m_identities;
    std::deque<Ack> m_acks;
    QJsonObject m_active;
    Execution m_execution = Execution::Idle;
    QElapsedTimer m_actionClock;
    bool m_transitionStarted = false;
    double m_lastCrossfader = 0;
    QString m_sourceIdentity;
    std::unique_ptr<QFutureWatcher<HashResult>> m_hashWatcher;
    std::shared_ptr<std::atomic_bool> m_hashCancelled;
    bool m_started = false;
};

}
