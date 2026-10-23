// Invented data, as the design README insists: the screens must never carry a
// real name or a real meeting. Shapes follow the mockups and the protocol
// schema (draft → review → approved) so wiring the API later is a swap of the
// source, not of the screens.

enum ProtocolStatus { draft, review, approved }

class Attendee {
  const Attendee(this.name, {this.initials = '', this.signed = false});
  final String name;
  final String initials;
  final bool signed;
}

class Decision {
  const Decision({required this.text, required this.owner, required this.due});
  final String text, owner, due;
}

class Point {
  const Point({required this.n, required this.title, required this.heard, required this.decided, required this.resolved});
  final int n;
  final String title;
  final List<(String, String)> heard; // (text, timecode)
  final String decided;
  final List<Decision> resolved;
}

class Meeting {
  const Meeting({required this.date, required this.number, required this.status, required this.topics, this.chair = 'Андрій Кравченко', this.secretary = 'Ігор Мельник', this.present = 7, this.total = 9, this.duration = '2:58:21', this.sizeMb = 78, this.signed = 0});
  final String date, number;
  final ProtocolStatus status;
  final int topics, present, total, signed;
  final String chair, secretary, duration;
  final int sizeMb;
}

const meetings = [
  Meeting(date: '17 серпня 2026', number: '17-08-2026/16', status: ProtocolStatus.approved, topics: 32, duration: '2:41:07', sizeMb: 71, signed: 7),
  Meeting(date: '3 серпня 2026', number: '03-08-2026/15', status: ProtocolStatus.approved, topics: 24, duration: '2:12:55', sizeMb: 58, signed: 6),
  Meeting(date: '30 липня 2026', number: '30-07-2026/15', status: ProtocolStatus.draft, topics: 29, duration: '3:04:18', sizeMb: 81),
  Meeting(date: '27 липня 2026', number: '27-07-2026/14', status: ProtocolStatus.review, topics: 33, duration: '2:58:21', sizeMb: 78, signed: 3),
  Meeting(date: '2 липня 2026', number: '02-07-2026/13', status: ProtocolStatus.approved, topics: 20, duration: '1:52:40', sizeMb: 49, signed: 8),
  Meeting(date: '27 червня 2026', number: '27-06-2026/12', status: ProtocolStatus.approved, topics: 38, duration: '3:11:02', sizeMb: 84, signed: 7),
  Meeting(date: '22 червня 2026', number: '22-06-2026/11', status: ProtocolStatus.approved, topics: 36),
  Meeting(date: '8 червня 2026', number: '08-06-2026/10', status: ProtocolStatus.approved, topics: 21),
  Meeting(date: '1 червня 2026', number: '01-06-2026/9', status: ProtocolStatus.approved, topics: 35),
];

const attendees = [
  Attendee('Андрій Кравченко', initials: 'АК', signed: true),
  Attendee('Ігор Мельник', initials: 'ІМ'),
  Attendee('Олена Ткаченко', initials: 'ОТ', signed: true),
  Attendee('Тарас Бондар', initials: 'ТБ', signed: true),
  Attendee('Сергій Коваль', initials: 'СК'),
  Attendee('Марія Шевчук', initials: 'МШ'),
  Attendee('Данило Гринь', initials: 'ДГ'),
];

const protocolPoints = [
  Point(
    n: 1,
    title: 'Регулярні зустрічі керівників напрямків',
    heard: [
      ('Потреба в регулярних зборах — раз на місяць, для навчання, молитви та поточних питань', '1:49:58'),
      ('Формат: спільні зустрічі або особисті бесіди з кожним керівником', '1:56:12'),
      ('Ювілей громади як привід згуртувати керівників навколо спільної мети', '2:03:40'),
    ],
    decided: 'Проводити зустрічі керівників раз на місяць; дату першої зафіксувати на початок місяця.',
    resolved: [Decision(text: 'Погодити дату зустрічі та повідомити керівників напрямків', owner: 'Андрій К.', due: 'до 12 вересня')],
  ),
  Point(
    n: 2,
    title: 'Планування Хрещення та святкування',
    heard: [
      ('Пропозиція поєднати хрещення з ювілеєм громади у вересні', '0:41:10'),
      ('Потрібен список кандидатів і дві співбесіди до кінця серпня', '0:47:02'),
    ],
    decided: 'Хрещення провести 27 вересня разом із святкуванням; співбесіди завершити до 31 серпня.',
    resolved: [
      Decision(text: 'Скласти список кандидатів на хрещення', owner: 'Олена Т.', due: 'до 20 серпня'),
      Decision(text: 'Узгодити програму святкування з музичним служінням', owner: 'Тарас Б.', due: 'до 5 вересня'),
    ],
  ),
  Point(
    n: 3,
    title: 'Оновлення складу господарського комітету',
    heard: [('Оновити склад комітету й описати його завдання', '1:12:04'), ('Описати обовʼязки координатора господарських питань', '1:18:31')],
    decided: 'Затвердити новий склад із трьох осіб; положення про комітет винести на наступну зустріч.',
    resolved: [Decision(text: 'Підготувати чернетку положення про господарський комітет', owner: 'Сергій К.', due: 'до 1 вересня')],
  ),
];

const topics = [
  'Розподіл опікунства над напрямками служінь', 'Планування Хрещення та святкування',
  'Стандарти підготовки та духовне наставництво для нових членів церкви', 'Питання від Тараса Б.',
  'Порушення порядку на території церкви', 'Парадигма служіння (Колоссян 3:23)', 'Регулярні зустрічі керівників напрямків',
  'Пропозиція від партнерів', 'Пасторська рада', 'Вступна молитва та духовний настрій', 'Бюджет дитячого служіння',
  'Оновлення складу господарського комітету', 'Молодіжний табір: дати та відповідальні',
];

const transcript = [
  ('Ігор Мельник', '00:00', 'Добре, починаємо.'),
  ('Андрій Кравченко', '00:09', 'Дякую всім, що попри зайнятість, служіння, роботи, Бог дає сили знаходити час. Сподіваюся, що це саме тому, що ми розуміємо, що ми все це робимо для Божої слави.'),
  ('Андрій Кравченко', '01:02', 'І роздумуючи над одним із текстів сьогодні, теж думав про парадигму служіння, коли в центрі уваги все ж таки Господь, а не просто люди.'),
  ('Олена Ткаченко', '02:15', 'Хочу одразу підняти питання хрещення: у нас є четверо кандидатів, і було б добре поєднати це зі святом.'),
  ('Тарас Бондар', '03:40', 'Музичне служіння готове, але потрібно знати дату хоча б за місяць.'),
  ('Ігор Мельник', '04:05', 'Пропоную зафіксувати 27 вересня і від цього рахувати назад усі терміни.'),
];

class TaskItem {
  const TaskItem({required this.text, required this.owner, required this.due, required this.meeting, required this.status});
  final String text, owner, due, meeting, status; // status: 'нова' | 'в роботі' | 'виконано' | 'прострочено'
}

const tasks = [
  TaskItem(text: 'Погодити дату зустрічі та повідомити керівників напрямків', owner: 'Андрій К.', due: '12 вер', meeting: '27 липня', status: 'в роботі'),
  TaskItem(text: 'Скласти список кандидатів на хрещення', owner: 'Олена Т.', due: '20 сер', meeting: '27 липня', status: 'виконано'),
  TaskItem(text: 'Узгодити програму святкування з музичним служінням', owner: 'Тарас Б.', due: '5 вер', meeting: '27 липня', status: 'нова'),
  TaskItem(text: 'Підготувати чернетку положення про господарський комітет', owner: 'Сергій К.', due: '1 вер', meeting: '27 липня', status: 'прострочено'),
  TaskItem(text: 'Звіт про надходження та витрати до членського зібрання', owner: 'Данило Г.', due: '30 сер', meeting: '22 червня', status: 'в роботі'),
  TaskItem(text: 'Табель чергувань на території на вересень', owner: 'Марія Ш.', due: '28 сер', meeting: '3 серпня', status: 'виконано'),
];

const recentQueries = ['де ми говорили про склад комітету?', 'Який бюджет для дитячого служіння?', 'На яких зустрічах ми говорили про музичне служіння?'];

class SearchHit {
  const SearchHit({required this.date, required this.score, required this.title, required this.lines});
  final String date;
  final double score;
  final String title;
  final List<(String, String)> lines;
}

const searchHits = [
  SearchHit(date: '30 липня 2026', score: 0.87, title: 'Оновлення складу господарського комітету', lines: [('Оновити склад комітету й описати його завдання', '1:12:04'), ('Описати обовʼязки координатора господарських питань', '1:18:31')]),
  SearchHit(date: '4 травня 2026', score: 0.81, title: 'Уточнення обовʼязків у Статуті громади', lines: [('Розмежувати духовні та організаційні обовʼязки', '42:17')]),
  SearchHit(date: '30 березня 2026', score: 0.64, title: 'Розподіл відповідальності між служіннями', lines: [('Хто відповідає за господарські питання між зустрічами', '1:03:55')]),
];
