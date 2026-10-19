#include "agent/agentbridge.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkProxy>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QtConcurrentRun>
#include <algorithm>
#include <cmath>

#include "moc_agentbridge.cpp"

namespace mixxx {
namespace {

const QRegularExpression kTrackId(QStringLiteral("^[a-f0-9]{64}$"));

bool near(double a, double b, double tolerance = 0.001) {
    return std::isfinite(a) && std::isfinite(b) && std::abs(a - b) <= tolerance;
}

QJsonObject deckState(const QJsonObject& state, int deck) {
    return state.value(QStringLiteral("decks")).toObject().value(QString::number(deck)).toObject();
}

bool validLoopbackUrl(const QUrl& url) {
    return url.isValid() && url.scheme() == QStringLiteral("http") &&
            (url.host() == QStringLiteral("127.0.0.1") || url.host() == QStringLiteral("::1")) &&
            url.port(8765) > 0 && url.port(8765) <= 65535 && url.userInfo().isEmpty() &&
            !url.hasQuery() && !url.hasFragment() &&
            (url.path().isEmpty() || url.path() == QStringLiteral("/"));
}

}

std::optional<AgentBridgeConfig> AgentBridgeConfig::fromFile(const QString& path, QString* pError) {
    auto fail = [pError](const QString& error) -> std::optional<AgentBridgeConfig> {
        *pError = error;
        return std::nullopt;
    };
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() > 8192) {
        return fail(QStringLiteral("Cannot read bridge configuration"));
    }
    const auto document = QJsonDocument::fromJson(file.readAll());
    const auto object = document.object();
    if (!document.isObject() || object.size() != 2 ||
            !object.value(QStringLiteral("backend_url")).isString() ||
            !object.value(QStringLiteral("token_file")).isString()) {
        return fail(QStringLiteral("Expected backend_url and token_file in bridge configuration"));
    }
    AgentBridgeConfig config;
    config.backendUrl = QUrl(object.value(QStringLiteral("backend_url")).toString());
    if (!validLoopbackUrl(config.backendUrl)) {
        return fail(QStringLiteral("Bridge backend must be a literal loopback HTTP address"));
    }
    const auto tokenPath = QFileInfo(path).absoluteDir().absoluteFilePath(
            object.value(QStringLiteral("token_file")).toString());
    const QFileInfo tokenInfo(tokenPath);
    const auto sharedPermissions = QFileDevice::ReadGroup | QFileDevice::WriteGroup |
            QFileDevice::ReadOther | QFileDevice::WriteOther;
    if (tokenInfo.isSymLink() || !tokenInfo.isFile() ||
            (tokenInfo.permissions() & sharedPermissions)) {
        return fail(QStringLiteral("Bridge token must be a private regular file"));
    }
    QFile tokenFile(tokenPath);
    if (!tokenFile.open(QIODevice::ReadOnly) || tokenFile.size() > 1024) {
        return fail(QStringLiteral("Cannot read bridge token"));
    }
    config.token = tokenFile.readAll().trimmed();
    const QRegularExpression tokenPattern(QStringLiteral("^[A-Za-z0-9_-]{32,256}$"));
    if (!tokenPattern.match(QString::fromLatin1(config.token)).hasMatch()) {
        return fail(QStringLiteral("Invalid bridge token file"));
    }
    return config;
}

AgentBridge::AgentBridge(AgentBridgeConfig config,
        std::unique_ptr<AgentPlayer> player,
        QNetworkAccessManager* pNetwork)
        : m_config(std::move(config)),
          m_player(std::move(player)),
          m_pNetwork(pNetwork ? pNetwork : &m_network) {
    m_pNetwork->setProxy(QNetworkProxy::NoProxy);
    m_tickTimer.setInterval(25);
    m_requestTimeout.setSingleShot(true);
    m_requestTimeout.setInterval(2000);
    connect(&m_tickTimer, &QTimer::timeout, this, &AgentBridge::tick);
    connect(&m_requestTimeout, &QTimer::timeout, this, [this] {
        if (m_pReply) {
            m_pReply->abort();
        }
    });
}

AgentBridge::~AgentBridge() {
    stop();
}

void AgentBridge::start() {
    if (m_started || !validLoopbackUrl(m_config.backendUrl) || m_config.token.size() < 32) {
        return;
    }
    m_started = true;
    m_nextRequest = 0;
    m_nextState = 0;
    m_clock.start();
    m_tickTimer.start();
    qInfo() << "Agent bridge enabled for local backend";
}

void AgentBridge::cancelExecution() {
    if (m_hashCancelled) {
        m_hashCancelled->store(true);
    }
    m_hashWatcher.reset();
    if (m_execution == Execution::Loading) {
        m_player->cancelLoads();
    }
    m_execution = Execution::Idle;
    m_active = {};
}

void AgentBridge::stop() {
    m_started = false;
    m_tickTimer.stop();
    m_requestTimeout.stop();
    cancelExecution();
    if (m_pReply) {
        disconnect(m_pReply, nullptr, this, nullptr);
        m_pReply->abort();
        m_pReply->deleteLater();
        m_pReply.clear();
    }
    m_instance.clear();
    m_acks.clear();
}

QJsonObject AgentBridge::observedState() const {
    auto state = m_player->state();
    auto decks = state.value(QStringLiteral("decks")).toObject();
    for (int deck = 1; deck <= 2; ++deck) {
        auto object = decks.value(QString::number(deck)).toObject();
        const auto& identity = m_identities[deck - 1];
        const QFileInfo file(identity.path);
        const bool identified = !identity.id.isEmpty() && !m_player->loading(deck) &&
                identity.path == m_player->loadedPath(deck) && file.size() == identity.size &&
                file.lastModified().toMSecsSinceEpoch() == identity.modified;
        object.insert(QStringLiteral("track_id"),
                identified ? QJsonValue(identity.id) : QJsonValue(QJsonValue::Null));
        decks.insert(QString::number(deck), object);
    }
    state.insert(QStringLiteral("decks"), decks);
    return state;
}

QJsonObject AgentBridge::snapshot() {
    auto state = observedState();
    state.insert(QStringLiteral("sequence"), static_cast<double>(++m_sequence));
    return state;
}

void AgentBridge::post(Request type, const QString& path, const QJsonObject& body) {
    auto url = m_config.backendUrl;
    url.setPath(QStringLiteral("/api/v1/bridge/") + path);
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    request.setRawHeader("Authorization", QByteArray("Bearer ") + m_config.token);
    request.setAttribute(
            QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    m_request = type;
    m_response.clear();
    m_pReply = m_pNetwork->post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(m_pReply, &QNetworkReply::readyRead, this, [this] {
        m_response += m_pReply->readAll();
        if (m_response.size() > 262144) {
            m_pReply->abort();
        }
    });
    connect(m_pReply, &QNetworkReply::finished, this, &AgentBridge::replyFinished);
    m_requestTimeout.start();
}

void AgentBridge::resetConnection() {
    cancelExecution();
    m_acks.clear();
    m_instance.clear();
    m_nextRequest = m_clock.elapsed() + 2000;
    m_nextState = 0;
}

void AgentBridge::replyFinished() {
    if (!m_pReply) {
        return;
    }
    m_requestTimeout.stop();
    m_response += m_pReply->readAll();
    const int status = m_pReply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const bool transportOk = m_pReply->error() == QNetworkReply::NoError;
    const auto document = QJsonDocument::fromJson(m_response);
    const auto response = document.object();
    m_pReply->deleteLater();
    m_pReply.clear();
    if (!transportOk || status < 200 || status >= 300 || m_response.size() > 262144 ||
            !document.isObject()) {
        qWarning() << "Agent bridge request failed; HTTP status" << status;
        if (m_execution != Execution::Idle) {
            finish(false, QStringLiteral("BACKEND_DISCONNECTED"));
        }
        if (status == 401 || (status == 409 && m_request != Request::Register)) {
            resetConnection();
        }
        m_nextRequest = m_clock.elapsed() + 2000;
        return;
    }
    switch (m_request) {
    case Request::Register:
        m_instance = response.value(QStringLiteral("instance_id")).toString();
        if (m_instance.isEmpty() || m_instance.size() > 64) {
            resetConnection();
        } else {
            m_nextState = 0;
        }
        break;
    case Request::State:
        m_nextState = m_clock.elapsed() + 1000;
        break;
    case Request::Claim:
        if (response.value(QStringLiteral("command")).isObject()) {
            execute(response.value(QStringLiteral("command")).toObject());
        }
        break;
    case Request::Ack:
        if (!m_acks.empty()) {
            m_acks.pop_front();
        }
        break;
    }
}

void AgentBridge::tick() {
    if (!m_started || !m_player->available()) {
        return;
    }
    advance();
    if (m_pReply || m_clock.elapsed() < m_nextRequest) {
        return;
    }
    m_nextRequest = m_clock.elapsed() + 250;
    if (m_instance.isEmpty()) {
        post(Request::Register,
                QStringLiteral("register"),
                {{QStringLiteral("protocol_version"), 1},
                        {QStringLiteral("name"), QStringLiteral("Mixxx native bridge")},
                        {QStringLiteral("capabilities"),
                                QJsonArray::fromStringList(m_player->capabilities())}});
    } else if (!m_acks.empty()) {
        post(Request::Ack,
                QStringLiteral("commands/%1/ack").arg(m_acks.front().commandId),
                m_acks.front().body);
    } else if (m_clock.elapsed() >= m_nextState) {
        post(Request::State,
                QStringLiteral("state"),
                {{QStringLiteral("instance_id"), m_instance},
                        {QStringLiteral("state"), snapshot()}});
    } else {
        post(Request::Claim,
                QStringLiteral("commands/claim"),
                {{QStringLiteral("instance_id"), m_instance}});
    }
}

QString AgentBridge::validate(const QJsonObject& payload) const {
    const auto operation = payload.value(QStringLiteral("command_type")).toString();
    if (!m_player->capabilities().contains(operation)) {
        return QStringLiteral("UNSUPPORTED_COMMAND");
    }
    const bool needsDeck = operation != QStringLiteral("stop") &&
            operation != QStringLiteral("set_effect") &&
            operation != QStringLiteral("set_crossfader");
    if (needsDeck &&
            (!payload.value(QStringLiteral("deck")).isDouble() ||
                    (payload.value(QStringLiteral("deck")).toDouble() != 1 &&
                            payload.value(QStringLiteral("deck")).toDouble() != 2))) {
        return QStringLiteral("INVALID_DECK");
    }
    if (operation == QStringLiteral("load_track")) {
        if (!kTrackId.match(payload.value(QStringLiteral("track_id")).toString()).hasMatch() ||
                !QFileInfo(payload.value(QStringLiteral("path")).toString()).isAbsolute()) {
            return QStringLiteral("INVALID_TRACK");
        }
    }
    if (operation == QStringLiteral("set_playing") &&
            !payload.value(QStringLiteral("playing")).isBool()) {
        return QStringLiteral("INVALID_PLAY_STATE");
    }
    if ((operation == QStringLiteral("set_sync") || operation == QStringLiteral("set_effect")) &&
            !payload.value(QStringLiteral("enabled")).isBool()) {
        return QStringLiteral("INVALID_ENABLED_STATE");
    }
    if (operation == QStringLiteral("set_effect")) {
        const auto unit = payload.value(QStringLiteral("effect_unit"));
        if (!unit.isDouble() || unit.toDouble() < 1 || unit.toDouble() > 4 ||
                std::floor(unit.toDouble()) != unit.toDouble()) {
            return QStringLiteral("INVALID_EFFECT_UNIT");
        }
    }
    if (operation == QStringLiteral("transition")) {
        if (!kTrackId.match(payload.value(QStringLiteral("expected_track_id")).toString())
                        .hasMatch()) {
            return QStringLiteral("INVALID_TRACK_IDENTITY");
        }
        const auto duration = payload.value(QStringLiteral("duration_ms"));
        if (!duration.isDouble() || duration.toDouble() < 0 || duration.toDouble() > 30000 ||
                std::floor(duration.toDouble()) != duration.toDouble()) {
            return QStringLiteral("INVALID_DURATION");
        }
    }
    if (operation == QStringLiteral("set_eq") &&
            !QStringList{QStringLiteral("low"), QStringLiteral("mid"), QStringLiteral("high")}
                    .contains(payload.value(QStringLiteral("band")).toString())) {
        return QStringLiteral("INVALID_EQ_BAND");
    }
    if (operation == QStringLiteral("seek") || operation == QStringLiteral("set_bpm") ||
            operation == QStringLiteral("set_volume") ||
            operation == QStringLiteral("set_crossfader") ||
            operation == QStringLiteral("set_eq")) {
        const auto value = payload.value(QStringLiteral("value"));
        const double minimum = operation == QStringLiteral("set_bpm")
                ? 20
                : (operation == QStringLiteral("set_crossfader") ? -1 : 0);
        const double maximum = operation == QStringLiteral("set_bpm")
                ? 300
                : (operation == QStringLiteral("set_eq") ? 2 : 1);
        if (!value.isDouble() || !std::isfinite(value.toDouble()) || value.toDouble() < minimum ||
                value.toDouble() > maximum) {
            return QStringLiteral("INVALID_VALUE");
        }
    }
    return {};
}

void AgentBridge::execute(const QJsonObject& command) {
    const auto id = command.value(QStringLiteral("id")).toString();
    if (id.isEmpty() || id.size() > 64 ||
            command.value(QStringLiteral("instance_id")).toString() != m_instance ||
            m_seen.contains(id)) {
        return;
    }
    const auto payload = command.value(QStringLiteral("payload")).toObject();
    const auto operation = payload.value(QStringLiteral("command_type")).toString();
    if (m_execution != Execution::Idle) {
        if (operation != QStringLiteral("stop")) {
            return;
        }
        finish(false, QStringLiteral("CANCELLED_BY_STOP"));
    }
    if (m_seen.size() >= 10000 && operation != QStringLiteral("stop")) {
        qWarning() << "Agent bridge command limit reached; restart Mixxx before further control";
        return;
    }
    m_seen.insert(id);
    m_active = command;
    m_execution = Execution::Settling;
    m_actionClock.start();
    const auto error = validate(payload);
    if (!error.isEmpty()) {
        finish(false, error);
        return;
    }
    const auto deadline = command.value(QStringLiteral("deadline"));
    const double now = QDateTime::currentMSecsSinceEpoch() / 1000.0;
    if (!deadline.isDouble() || deadline.toDouble() <= now || deadline.toDouble() > now + 60) {
        finish(false, QStringLiteral("COMMAND_EXPIRED"));
        return;
    }
    const int deck = payload.value(QStringLiteral("deck")).toInt();
    const auto state = observedState();
    const auto current = deckState(state, deck);
    if (payload.contains(QStringLiteral("expected_track_id")) &&
            payload.value(QStringLiteral("expected_track_id")) !=
                    current.value(QStringLiteral("track_id"))) {
        finish(false, QStringLiteral("TRACK_CHANGED"));
        return;
    }
    if (operation == QStringLiteral("stop")) {
        m_player->cancelLoads();
        m_player->setValue(1, QStringLiteral("play"), 0);
        m_player->setValue(2, QStringLiteral("play"), 0);
    } else if (operation == QStringLiteral("load_track")) {
        if (current.value(QStringLiteral("playing")).toBool() || m_player->loading(deck)) {
            finish(false, QStringLiteral("DECK_BUSY"));
        } else {
            verifyLoad(payload);
        }
    } else if (operation == QStringLiteral("set_playing")) {
        if (payload.value(QStringLiteral("playing")).toBool() &&
                (current.value(QStringLiteral("track_id")).isNull() || m_player->loading(deck))) {
            finish(false, QStringLiteral("DECK_EMPTY"));
        } else {
            m_player->setValue(deck,
                    QStringLiteral("play"),
                    payload.value(QStringLiteral("playing")).toBool() ? 1 : 0);
        }
    } else if (operation == QStringLiteral("transition")) {
        const auto source = deckState(state, 3 - deck);
        const double start = deck == 1 ? 1 : -1;
        if (!source.value(QStringLiteral("playing")).toBool() ||
                current.value(QStringLiteral("playing")).toBool() ||
                current.value(QStringLiteral("track_id")).isNull() ||
                source.value(QStringLiteral("track_id")).isNull() || m_player->loading(deck) ||
                m_player->loading(3 - deck) || !m_player->transitionReady() ||
                !near(state.value(QStringLiteral("crossfader")).toDouble(), start)) {
            finish(false, QStringLiteral("TRANSITION_STATE"));
        } else {
            m_lastCrossfader = start;
            m_sourceIdentity = source.value(QStringLiteral("track_id")).toString();
            m_player->setValue(deck, QStringLiteral("volume"), 1);
            m_player->setValue(deck, QStringLiteral("play"), 1);
            m_transitionStarted = false;
            m_execution = Execution::Transition;
        }
    } else if (operation == QStringLiteral("set_effect")) {
        m_player->setEffect(payload.value(QStringLiteral("effect_unit")).toInt(),
                payload.value(QStringLiteral("enabled")).toBool());
    } else {
        QString key;
        double value = payload.value(QStringLiteral("value")).toDouble();
        if (operation == QStringLiteral("seek")) {
            key = QStringLiteral("playposition");
        } else if (operation == QStringLiteral("set_bpm")) {
            key = QStringLiteral("bpm");
        } else if (operation == QStringLiteral("set_volume")) {
            key = QStringLiteral("volume");
        } else if (operation == QStringLiteral("set_crossfader")) {
            key = QStringLiteral("crossfader");
        } else if (operation == QStringLiteral("set_sync")) {
            key = QStringLiteral("sync_enabled");
            value = payload.value(QStringLiteral("enabled")).toBool() ? 1 : 0;
        } else if (operation == QStringLiteral("set_eq")) {
            const auto band = payload.value(QStringLiteral("band")).toString();
            key = band == QStringLiteral("low")
                    ? QStringLiteral("filterLow")
                    : (band == QStringLiteral("mid") ? QStringLiteral("filterMid")
                                                     : QStringLiteral("filterHigh"));
        }
        if (!m_player->setValue(
                    operation == QStringLiteral("set_crossfader") ? 0 : deck, key, value)) {
            finish(false, QStringLiteral("CONTROL_UNAVAILABLE"));
        }
    }
}

void AgentBridge::verifyLoad(const QJsonObject& payload) {
    m_execution = Execution::Verifying;
    const auto path = payload.value(QStringLiteral("path")).toString();
    const auto id = payload.value(QStringLiteral("track_id")).toString();
    const auto commandId = m_active.value(QStringLiteral("id")).toString();
    m_hashCancelled = std::make_shared<std::atomic_bool>(false);
    const auto cancelled = m_hashCancelled;
    m_hashWatcher = std::make_unique<QFutureWatcher<HashResult>>();
    connect(m_hashWatcher.get(),
            &QFutureWatcher<HashResult>::finished,
            this,
            [this, commandId, payload] {
                if (m_active.value(QStringLiteral("id")).toString() != commandId ||
                        m_execution != Execution::Verifying) {
                    return;
                }
                const auto result = m_hashWatcher->result();
                const int deck = payload.value(QStringLiteral("deck")).toInt();
                if (!result.valid || m_player->loading(deck) ||
                        deckState(observedState(), deck)
                                .value(QStringLiteral("playing"))
                                .toBool()) {
                    finish(false, QStringLiteral("FILE_CHANGED_OR_DECK_BUSY"));
                    return;
                }
                m_identities[deck - 1] = result.identity;
                m_execution = Execution::Loading;
                m_player->load(deck, result.identity.path);
            });
    m_hashWatcher->setFuture(QtConcurrent::run([path, id, cancelled] {
        HashResult result;
        const QFileInfo before(path);
        QFile file(before.canonicalFilePath());
        if (!before.isFile() || !file.open(QIODevice::ReadOnly)) {
            return result;
        }
        QCryptographicHash hash(QCryptographicHash::Sha256);
        while (!file.atEnd() && !cancelled->load()) {
            const auto bytes = file.read(262144);
            if (bytes.isEmpty() && file.error() != QFileDevice::NoError) {
                return result;
            }
            hash.addData(bytes);
        }
        const QFileInfo after(path);
        result.identity = {before.canonicalFilePath(),
                id,
                before.size(),
                before.lastModified().toMSecsSinceEpoch()};
        result.valid = !cancelled->load() && hash.result().toHex() == id.toLatin1() &&
                before.size() == after.size() && before.lastModified() == after.lastModified() &&
                before.canonicalFilePath() == after.canonicalFilePath();
        return result;
    }));
}

bool AgentBridge::matches(const QJsonObject& payload, const QJsonObject& state) const {
    const auto operation = payload.value(QStringLiteral("command_type")).toString();
    const int deck = payload.value(QStringLiteral("deck")).toInt();
    const auto current = deckState(state, deck);
    if (payload.contains(QStringLiteral("expected_track_id")) &&
            payload.value(QStringLiteral("expected_track_id")) !=
                    current.value(QStringLiteral("track_id"))) {
        return false;
    }
    if (operation == QStringLiteral("stop")) {
        return !deckState(state, 1).value(QStringLiteral("playing")).toBool() &&
                !deckState(state, 2).value(QStringLiteral("playing")).toBool();
    }
    if (operation == QStringLiteral("load_track")) {
        return !m_player->loading(deck) &&
                current.value(QStringLiteral("track_id")) ==
                payload.value(QStringLiteral("track_id")) &&
                !current.value(QStringLiteral("playing")).toBool();
    }
    if (operation == QStringLiteral("set_playing")) {
        return current.value(QStringLiteral("playing")) == payload.value(QStringLiteral("playing"));
    }
    if (operation == QStringLiteral("set_sync")) {
        return current.value(QStringLiteral("sync_enabled")) ==
                payload.value(QStringLiteral("enabled"));
    }
    if (operation == QStringLiteral("set_effect")) {
        return state.value(QStringLiteral("effects"))
                       .toObject()
                       .value(QString::number(
                               payload.value(QStringLiteral("effect_unit")).toInt())) ==
                payload.value(QStringLiteral("enabled"));
    }
    if (operation == QStringLiteral("transition")) {
        return current.value(QStringLiteral("playing")).toBool() &&
                near(current.value(QStringLiteral("volume")).toDouble(), 1) &&
                !deckState(state, 3 - deck).value(QStringLiteral("playing")).toBool() &&
                near(state.value(QStringLiteral("crossfader")).toDouble(), deck == 1 ? -1 : 1);
    }
    if (operation == QStringLiteral("set_eq")) {
        return near(current.value(QStringLiteral("eq"))
                            .toObject()
                            .value(payload.value(QStringLiteral("band")).toString())
                            .toDouble(),
                payload.value(QStringLiteral("value")).toDouble());
    }
    if (operation == QStringLiteral("set_crossfader")) {
        return near(state.value(QStringLiteral("crossfader")).toDouble(),
                payload.value(QStringLiteral("value")).toDouble());
    }
    const auto key = operation == QStringLiteral("seek")
            ? QStringLiteral("position")
            : (operation == QStringLiteral("set_bpm") ? QStringLiteral("bpm")
                                                      : QStringLiteral("volume"));
    return near(current.value(key).toDouble(),
            payload.value(QStringLiteral("value")).toDouble(),
            operation == QStringLiteral("seek") ? 0.01 : 0.001);
}

void AgentBridge::advance() {
    if (m_execution == Execution::Idle) {
        return;
    }
    if (QDateTime::currentMSecsSinceEpoch() / 1000.0 >=
            m_active.value(QStringLiteral("deadline")).toDouble()) {
        finish(false, QStringLiteral("COMMAND_TIMEOUT"));
        return;
    }
    const auto payload = m_active.value(QStringLiteral("payload")).toObject();
    const auto state = observedState();
    if (m_execution == Execution::Transition) {
        const int deck = payload.value(QStringLiteral("deck")).toInt();
        const auto current = deckState(state, deck);
        const auto source = deckState(state, 3 - deck);
        if (current.value(QStringLiteral("track_id")) !=
                        payload.value(QStringLiteral("expected_track_id")) ||
                source.value(QStringLiteral("track_id")).toString() != m_sourceIdentity ||
                !source.value(QStringLiteral("playing")).toBool() || !m_player->transitionReady() ||
                !near(state.value(QStringLiteral("crossfader")).toDouble(), m_lastCrossfader)) {
            finish(false, QStringLiteral("TRANSITION_INTERRUPTED"));
            return;
        }
        if (!current.value(QStringLiteral("playing")).toBool()) {
            if (m_transitionStarted || m_actionClock.elapsed() > 2000) {
                finish(false, QStringLiteral("TRANSITION_DESTINATION_STOPPED"));
            }
            return;
        }
        if (!m_transitionStarted) {
            m_transitionStarted = true;
            m_actionClock.restart();
        }
        const int duration = payload.value(QStringLiteral("duration_ms")).toInt();
        const double progress = duration == 0
                ? 1
                : std::min(1.0, m_actionClock.elapsed() / static_cast<double>(duration));
        const double target = deck == 1 ? -1 : 1;
        m_lastCrossfader = -target + 2 * target * progress;
        m_player->setValue(0, QStringLiteral("crossfader"), m_lastCrossfader);
        if (progress >= 1) {
            m_player->setValue(3 - deck, QStringLiteral("play"), 0);
            m_execution = Execution::Settling;
        }
        return;
    }
    if ((m_execution == Execution::Settling || m_execution == Execution::Loading) &&
            matches(payload, state)) {
        finish(true);
    }
}

void AgentBridge::finish(bool success, const QString& error) {
    if (m_active.isEmpty()) {
        return;
    }
    const auto id = m_active.value(QStringLiteral("id")).toString();
    if (!success) {
        qWarning() << "Agent bridge command failed:" << error;
        if (m_hashCancelled) {
            m_hashCancelled->store(true);
        }
        if (m_execution == Execution::Loading) {
            m_player->cancelLoads();
        }
    }
    m_execution = Execution::Idle;
    m_active = {};
    m_acks.push_back({id,
            {{QStringLiteral("instance_id"), m_instance},
                    {QStringLiteral("success"), success},
                    {QStringLiteral("error"),
                            error.isEmpty() ? QJsonValue(QJsonValue::Null) : QJsonValue(error)},
                    {QStringLiteral("state"), snapshot()}}});
}

}
