// Скрипт медиа-типа Zabbix (тип «Webhook») для отправки проблем в «Дежурку».
//
// Параметры медиа-типа (Administration → Media types → Create → Type: Webhook):
//   url          https://dezhurka.example.ru/api/v1/ingest/<integration_key>/zabbix
//   event_id     {EVENT.ID}
//   event_value  {EVENT.VALUE}
//   status       {EVENT.STATUS}
//   severity     {EVENT.SEVERITY}
//   host         {HOST.NAME}
//   trigger_id   {TRIGGER.ID}
//   trigger_name {EVENT.NAME}
//   message      {ALERT.MESSAGE}
//
// В действии (Trigger action) включите и Operations, и Recovery operations —
// тогда «Дежурка» сама закроет инцидент, когда проблема уйдёт.

var params = JSON.parse(value);
if (!params.url) {
    throw 'Не задан параметр url';
}

var payload = {};
['event_id', 'event_value', 'status', 'severity', 'host', 'trigger_id', 'trigger_name', 'message'].forEach(function (k) {
    if (params[k] !== undefined && params[k] !== '' && params[k].indexOf('{') !== 0) {
        payload[k] = params[k];
    }
});

var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
if (params.HTTPProxy) {
    req.setProxy(params.HTTPProxy);
}
var resp = req.post(params.url, JSON.stringify(payload));
var code = req.getStatus();
if (code < 200 || code >= 300) {
    throw 'Дежурка ответила ' + code + ': ' + resp;
}
return 'OK';
