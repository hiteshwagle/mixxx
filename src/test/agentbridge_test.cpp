#include "agent/agentbridge.h"

#include <gtest/gtest.h>

#include <QCryptographicHash>
#include <QDateTime>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkRequest>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <deque>
#include <functional>

#include "test/mixxxtest.h"
#include "test/mock_networkaccessmanager.h"

namespace {

class BridgePlayer final : public mixxx::AgentPlayer {
  public:
    BridgePlayer() {
        for (int deck = 1; deck <= 2; ++deck) {
            decks.insert(QString::number(deck),
                    QJsonObject{{QStringLiteral("playing"), false},
                            {QStringLiteral("position"), 0},
                            {QStringLiteral("bpm"), 120},
                            {QStringLiteral("volume"), 1},
                            {QStringLiteral("sync_enabled"), false},
                            {QStringLiteral("eq"),
                                    QJsonObject{{QStringLiteral("low"), 1},
                                            {QStringLiteral("mid"), 1},
                                            {QStringLiteral("high"), 1}}}});
        }
    }
    bool available() const override {
        return true;
    }
    QStringList capabilities() const override {
        return {QStringLiteral("load_track"),
                QStringLiteral("set_playing"),
                QStringLiteral("stop"),
                QStringLiteral("set_crossfader"),
                QStringLiteral("set_volume"),
                QStringLiteral("set_eq"),
                QStringLiteral("seek"),
                QStringLiteral("set_bpm"),
                QStringLiteral("set_sync"),
                QStringLiteral("set_effect"),
                QStringLiteral("transition")};
    }
    QJsonObject state() const override {
        return {{QStringLiteral("decks"), decks},
                {QStringLiteral("crossfader"), crossfader},
                {QStringLiteral("effects"), effects}};
    }
    QString loadedPath(int deck) const override {
        return paths[deck - 1];
    }
    bool loading(int deck) const override {
        return delayed && !paths[deck - 1].isEmpty();
    }
    bool transitionReady() const override {
        return routingReady;
    }
    bool setValue(int deck, const QString& key, double value) override {
        ++sets;
        if (ignoreSets) {
            return true;
        }
        if (key == QStringLiteral("crossfader")) {
            crossfader = value;
            return true;
        }
        auto current = decks.value(QString::number(deck)).toObject();
        if (key == QStringLiteral("play")) {
            current.insert(QStringLiteral("playing"), value > 0);
        } else if (key == QStringLiteral("playposition")) {
            current.insert(QStringLiteral("position"), value);
        } else if (key == QStringLiteral("sync_enabled")) {
            current.insert(key, value > 0);
        } else if (key.startsWith(QStringLiteral("filter"))) {
            auto eq = current.value(QStringLiteral("eq")).toObject();
            eq.insert(key.mid(6).toLower(), value);
            current.insert(QStringLiteral("eq"), eq);
        } else {
            current.insert(key, value);
        }
        decks.insert(QString::number(deck), current);
        return true;
    }
    bool setEffect(int unit, bool enabled) override {
        effects.insert(QString::number(unit), enabled);
        return true;
    }
    void load(int deck, const QString& path) override {
        ++loads;
        paths[deck - 1] = path;
    }
    void cancelLoads() override {
        ++cancels;
        if (delayed) {
            paths = {};
            delayed = false;
        }
    }
    QJsonObject decks;
    QJsonObject effects;
    std::array<QString, 2> paths;
    double crossfader = -1;
    int loads = 0;
    int sets = 0;
    int cancels = 0;
    bool delayed = false;
    bool ignoreSets = false;
    bool routingReady = true;
};

class BridgeNetwork final : public QNetworkAccessManager {
  public:
    std::deque<QJsonObject> commands;
    QList<QJsonObject> acknowledgements;
    QJsonObject lastState;
    bool redirect = false;
    bool failFirstAck = false;
    int requests = 0;
    bool authCorrect = true;
    bool sequenceIncreases = true;
    qint64 lastSequence = 0;

  protected:
    QNetworkReply* createRequest(
            Operation operation, const QNetworkRequest& request, QIODevice* data) override {
        ++requests;
        EXPECT_EQ(PostOperation, operation);
        authCorrect &=
                request.rawHeader("Authorization") == QByteArray("Bearer ") + QByteArray(40, 'x');
        EXPECT_EQ(QNetworkRequest::ManualRedirectPolicy,
                request.attribute(QNetworkRequest::RedirectPolicyAttribute).toInt());
        const auto body = QJsonDocument::fromJson(data->readAll()).object();
        const auto path = request.url().path();
        QJsonObject response;
        if (path.endsWith(QStringLiteral("/register"))) {
            response.insert(QStringLiteral("instance_id"), QStringLiteral("test-instance"));
        } else if (path.endsWith(QStringLiteral("/state")) ||
                path.endsWith(QStringLiteral("/ack"))) {
            const auto state = body.value(QStringLiteral("state")).toObject();
            const auto sequence =
                    static_cast<qint64>(state.value(QStringLiteral("sequence")).toDouble());
            sequenceIncreases &= sequence > lastSequence;
            lastSequence = sequence;
            lastState = state;
            if (path.endsWith(QStringLiteral("/ack"))) {
                acknowledgements.append(body);
            }
        } else if (path.endsWith(QStringLiteral("/claim"))) {
            if (!commands.empty()) {
                response.insert(QStringLiteral("command"), commands.front());
                commands.pop_front();
            } else {
                response.insert(QStringLiteral("command"), QJsonValue(QJsonValue::Null));
            }
        }
        const bool failAck = failFirstAck && path.endsWith(QStringLiteral("/ack"));
        if (failAck) {
            failFirstAck = false;
        }
        auto reply = std::make_unique<MockNetworkReply>(QJsonDocument(response).toJson());
        reply->setParent(this);
        reply->setAttribute(
                QNetworkRequest::HttpStatusCodeAttribute, failAck ? 500 : (redirect ? 302 : 200));
        auto* pReply = reply.release();
        QTimer::singleShot(0, pReply, [pReply] { pReply->Done(); });
        return pReply;
    }
};

bool waitFor(const std::function<bool()>& predicate, int timeout = 6000) {
    QElapsedTimer timer;
    timer.start();
    while (!predicate() && timer.elapsed() < timeout) {
        QTest::qWait(10);
    }
    return predicate();
}

}

class AgentBridgeTest : public MixxxTest {
  protected:
    void SetUp() override {
        auto player = std::make_unique<BridgePlayer>();
        m_pPlayer = player.get();
        m_bridge = std::make_unique<mixxx::AgentBridge>(
                mixxx::AgentBridgeConfig{
                        QUrl(QStringLiteral("http://127.0.0.1:8765")), QByteArray(40, 'x')},
                std::move(player),
                &m_network);
        m_bridge->start();
    }
    QJsonObject enqueue(const QJsonObject& payload, const QString& id = QString()) {
        const auto identifier = id.isEmpty() ? QString::number(++m_nextId) : id;
        QJsonObject command{{QStringLiteral("id"), identifier},
                {QStringLiteral("instance_id"), QStringLiteral("test-instance")},
                {QStringLiteral("deadline"), QDateTime::currentMSecsSinceEpoch() / 1000.0 + 45},
                {QStringLiteral("payload"), payload}};
        m_network.commands.push_back(command);
        return command;
    }
    QString loadTrack(int deck, const QString& contents) {
        const auto path = m_files.filePath(QString::number(deck) + QStringLiteral(".wav"));
        QFile file(path);
        if (!file.open(QIODevice::WriteOnly)) {
            ADD_FAILURE();
            return {};
        }
        file.write(contents.toUtf8());
        file.close();
        const auto id = QString::fromLatin1(
                QCryptographicHash::hash(contents.toUtf8(), QCryptographicHash::Sha256).toHex());
        const int previous = m_network.acknowledgements.size();
        enqueue({{QStringLiteral("command_type"), QStringLiteral("load_track")},
                {QStringLiteral("deck"), deck},
                {QStringLiteral("track_id"), id},
                {QStringLiteral("path"), path}});
        EXPECT_TRUE(
                waitFor([this, previous] { return m_network.acknowledgements.size() > previous; }));
        EXPECT_TRUE(m_network.acknowledgements.last().value(QStringLiteral("success")).toBool());
        return id;
    }
    BridgeNetwork m_network;
    BridgePlayer* m_pPlayer = nullptr;
    std::unique_ptr<mixxx::AgentBridge> m_bridge;
    QTemporaryDir m_files;
    int m_nextId = 0;
};

TEST_F(AgentBridgeTest, ReportsActualStateAndNeverReexecutesCommand) {
    const auto command =
            enqueue({{QStringLiteral("command_type"), QStringLiteral("set_crossfader")},
                    {QStringLiteral("value"), 0.5}});
    ASSERT_TRUE(waitFor([this] { return !m_network.acknowledgements.isEmpty(); }));
    EXPECT_DOUBLE_EQ(0.5, m_pPlayer->crossfader);
    EXPECT_TRUE(m_network.acknowledgements.first().value(QStringLiteral("success")).toBool());
    const int sets = m_pPlayer->sets;
    m_network.commands.push_back(command);
    QTest::qWait(1200);
    EXPECT_EQ(sets, m_pPlayer->sets);
    EXPECT_EQ(1, m_network.acknowledgements.size());
    EXPECT_TRUE(m_network.authCorrect);
    EXPECT_TRUE(m_network.sequenceIncreases);
}

TEST_F(AgentBridgeTest, HashMustMatchBeforeLoad) {
    const auto path = m_files.filePath(QStringLiteral("changed.wav"));
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::WriteOnly));
    file.write("changed content");
    file.close();
    enqueue({{QStringLiteral("command_type"), QStringLiteral("load_track")},
            {QStringLiteral("deck"), 1},
            {QStringLiteral("path"), path},
            {QStringLiteral("track_id"), QString(64, QLatin1Char('a'))}});
    ASSERT_TRUE(waitFor([this] { return !m_network.acknowledgements.isEmpty(); }));
    EXPECT_FALSE(m_network.acknowledgements.first().value(QStringLiteral("success")).toBool());
    EXPECT_EQ(0, m_pPlayer->loads);
}

TEST_F(AgentBridgeTest, WaitsForNativeLoadCompletion) {
    m_pPlayer->delayed = true;
    const auto path = m_files.filePath(QStringLiteral("loading.wav"));
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::WriteOnly));
    file.write("audio");
    file.close();
    const auto id = QString::fromLatin1(
            QCryptographicHash::hash("audio", QCryptographicHash::Sha256).toHex());
    enqueue({{QStringLiteral("command_type"), QStringLiteral("load_track")},
            {QStringLiteral("deck"), 1},
            {QStringLiteral("path"), path},
            {QStringLiteral("track_id"), id}});
    ASSERT_TRUE(waitFor([this] { return m_pPlayer->loads == 1; }));
    QTest::qWait(500);
    EXPECT_TRUE(m_network.acknowledgements.isEmpty());
    m_pPlayer->delayed = false;
    ASSERT_TRUE(waitFor([this] { return !m_network.acknowledgements.isEmpty(); }));
    const auto state = m_network.acknowledgements.first().value(QStringLiteral("state")).toObject();
    EXPECT_EQ(id,
            state.value(QStringLiteral("decks"))
                    .toObject()
                    .value(QStringLiteral("1"))
                    .toObject()
                    .value(QStringLiteral("track_id"))
                    .toString());
}

TEST_F(AgentBridgeTest, TransitionCompletesLocallyAndStopsSource) {
    loadTrack(1, QStringLiteral("first track"));
    const auto destination = loadTrack(2, QStringLiteral("second track"));
    m_pPlayer->setValue(1, QStringLiteral("play"), 1);
    enqueue({{QStringLiteral("command_type"), QStringLiteral("transition")},
            {QStringLiteral("deck"), 2},
            {QStringLiteral("duration_ms"), 200},
            {QStringLiteral("expected_track_id"), destination}});
    ASSERT_TRUE(waitFor([this] { return m_network.acknowledgements.size() == 3; }));
    EXPECT_TRUE(m_network.acknowledgements.last().value(QStringLiteral("success")).toBool());
    EXPECT_DOUBLE_EQ(1, m_pPlayer->crossfader);
    EXPECT_FALSE(m_pPlayer->decks.value(QStringLiteral("1"))
                    .toObject()
                    .value(QStringLiteral("playing"))
                    .toBool());
    EXPECT_TRUE(m_pPlayer->decks.value(QStringLiteral("2"))
                    .toObject()
                    .value(QStringLiteral("playing"))
                    .toBool());
}

TEST_F(AgentBridgeTest, StopPreemptsTransitionAndCannotRestartLater) {
    loadTrack(1, QStringLiteral("first track"));
    const auto destination = loadTrack(2, QStringLiteral("second track"));
    m_pPlayer->setValue(1, QStringLiteral("play"), 1);
    enqueue({{QStringLiteral("command_type"), QStringLiteral("transition")},
            {QStringLiteral("deck"), 2},
            {QStringLiteral("duration_ms"), 3000},
            {QStringLiteral("expected_track_id"), destination}});
    ASSERT_TRUE(waitFor([this] {
        return m_pPlayer->decks.value(QStringLiteral("2"))
                .toObject()
                .value(QStringLiteral("playing"))
                .toBool();
    }));
    enqueue({{QStringLiteral("command_type"), QStringLiteral("stop")}});
    ASSERT_TRUE(waitFor([this] { return m_network.acknowledgements.size() == 4; }));
    const double stoppedFader = m_pPlayer->crossfader;
    QTest::qWait(3100);
    EXPECT_DOUBLE_EQ(stoppedFader, m_pPlayer->crossfader);
    EXPECT_FALSE(m_pPlayer->decks.value(QStringLiteral("1"))
                    .toObject()
                    .value(QStringLiteral("playing"))
                    .toBool());
    EXPECT_FALSE(m_pPlayer->decks.value(QStringLiteral("2"))
                    .toObject()
                    .value(QStringLiteral("playing"))
                    .toBool());
    EXPECT_FALSE(m_network.acknowledgements.at(2).value(QStringLiteral("success")).toBool());
    EXPECT_TRUE(m_network.acknowledgements.at(3).value(QStringLiteral("success")).toBool());
}

TEST_F(AgentBridgeTest, RejectsInvalidValuesAndExpiredCommands) {
    enqueue({{QStringLiteral("command_type"), QStringLiteral("set_volume")},
            {QStringLiteral("deck"), 1},
            {QStringLiteral("value"), 99}});
    enqueue({{QStringLiteral("command_type"), QStringLiteral("stop")}});
    m_network.commands.back().insert(QStringLiteral("deadline"), 1);
    ASSERT_TRUE(waitFor([this] { return m_network.acknowledgements.size() == 2; }));
    EXPECT_EQ(0, m_pPlayer->sets);
    EXPECT_FALSE(m_network.acknowledgements.at(0).value(QStringLiteral("success")).toBool());
    EXPECT_FALSE(m_network.acknowledgements.at(1).value(QStringLiteral("success")).toBool());
}

TEST_F(AgentBridgeTest, DoesNotFollowRedirectsOrSendCommandsToOtherHosts) {
    m_network.redirect = true;
    enqueue({{QStringLiteral("command_type"), QStringLiteral("stop")}});
    QTest::qWait(700);
    EXPECT_EQ(1, m_network.requests);
    EXPECT_EQ(0, m_pPlayer->sets);
}

TEST_F(AgentBridgeTest, RetriesAcknowledgementWithoutRepeatingPhysicalAction) {
    m_network.failFirstAck = true;
    enqueue({{QStringLiteral("command_type"), QStringLiteral("set_crossfader")},
            {QStringLiteral("value"), 0.5}});
    ASSERT_TRUE(waitFor([this] { return m_network.acknowledgements.size() >= 2; }));
    EXPECT_EQ(1, m_pPlayer->sets);
    EXPECT_EQ(m_network.acknowledgements.at(0), m_network.acknowledgements.at(1));
}

TEST_F(AgentBridgeTest, DoesNotEchoRequestedValuesAsObservedState) {
    m_pPlayer->ignoreSets = true;
    enqueue({{QStringLiteral("command_type"), QStringLiteral("set_crossfader")},
            {QStringLiteral("value"), 1}});
    m_network.commands.back().insert(
            QStringLiteral("deadline"), QDateTime::currentMSecsSinceEpoch() / 1000.0 + 1.5);
    ASSERT_TRUE(waitFor([this] { return !m_network.acknowledgements.isEmpty(); }));
    const auto ack = m_network.acknowledgements.first();
    EXPECT_FALSE(ack.value(QStringLiteral("success")).toBool());
    EXPECT_DOUBLE_EQ(-1,
            ack.value(QStringLiteral("state"))
                    .toObject()
                    .value(QStringLiteral("crossfader"))
                    .toDouble());
}

TEST_F(MixxxTest, AgentConfigRejectsRemoteHostsAndPublicTokens) {
    QTemporaryDir files;
    const auto configPath = files.filePath(QStringLiteral("config.json"));
    const auto tokenPath = files.filePath(QStringLiteral("bridge-token"));
    QFile token(tokenPath);
    ASSERT_TRUE(token.open(QIODevice::WriteOnly));
    token.write(QByteArray(40, 'x'));
    token.close();
    ASSERT_TRUE(token.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner));
    auto writeConfig = [&configPath](const QString& url) {
        QFile config(configPath);
        EXPECT_TRUE(config.open(QIODevice::WriteOnly | QIODevice::Truncate));
        config.write(QJsonDocument(
                QJsonObject{{QStringLiteral("backend_url"), url},
                        {QStringLiteral("token_file"), QStringLiteral("bridge-token")}})
                        .toJson());
    };
    QString error;
    writeConfig(QStringLiteral("http://example.com"));
    EXPECT_FALSE(mixxx::AgentBridgeConfig::fromFile(configPath, &error));
    writeConfig(QStringLiteral("http://127.0.0.1:8765"));
    EXPECT_TRUE(mixxx::AgentBridgeConfig::fromFile(configPath, &error));
    ASSERT_TRUE(token.setPermissions(
            QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ReadOther));
    EXPECT_FALSE(mixxx::AgentBridgeConfig::fromFile(configPath, &error));
}
