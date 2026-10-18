"""Edmonds–Karp: распределение часов без двойного счёта многоролевых людей.

Алгоритмический источник: CLRS, Introduction to Algorithms, глава Maximum Flow.
Реализация написана для проекта; внешние библиотеки оптимизации не нужны.
"""

from collections import deque

EPS = 1e-9


class Network:
    """
    Сеть «источник → люди → потребности → сток» с досчётом потока.

    Потребности добавляются по одной, в порядке приоритета, и после
    каждой поток увеличивается. Увеличивающий путь кончается на стоке,
    поэтому поток уже удовлетворённых потребностей не уменьшается,
    а люди при этом могут перейти на другие работы. Так новая работа
    с единственным возможным исполнителем получает его, даже если
    раньше его часы заняла работа, которую может сделать и кто-то другой.

    Две поправки к выбору людей, не меняющие объём потока:
    - preferred — люди, которых работа получает в первую очередь (своя
      команда); остальные подключаются методом widen, только если своих
      не хватило;
    - путь ищется сначала через человека с наибольшим свободным остатком,
      поэтому работа расходится по людям, а не копится на первых по номеру.
    """

    def __init__(self, capacities):
        self.graph = {}
        self.initial = {}
        self.capacities = capacities
        self.pending = []
        for eid, cap in sorted(capacities.items()):
            if cap > EPS:
                self._edge("source", ("person", eid), cap)

    def _edge(self, a, b, capacity):
        self.graph.setdefault(a, {})[b] = capacity
        self.graph.setdefault(b, {}).setdefault(a, 0.0)
        self.initial[a, b] = capacity

    def add(self, demand, hours, eligible, preferred=None, first=None):
        """Добавляет потребность и досчитывает поток.

        preferred — подмножество eligible, с которого начинается раскладка
        (своя команда); остальные ждут widen. first — ещё более узкий круг
        внутри preferred: люди, которые вели эту работу в прошлом спринте.
        Если он не пуст, сначала работа достаётся им, своя команда
        подключается через widen(1), остальные — через widen().
        """
        node = ("role", demand)
        self._edge(node, "sink", hours)
        home = set(eligible) if preferred is None else set(preferred) & set(eligible)
        lead = set(first or ()) & home
        for eid in sorted(eligible):
            if self.capacities.get(eid, 0) > EPS:
                tier = 0 if (eid in lead or (not lead and eid in home)) else 1 if eid in home else 2
                if tier == 0:
                    self._edge(("person", eid), node, hours)
                else:
                    self.pending.append((tier, eid, node, hours))
        self._augment()

    def widen(self, level=2):
        """Подключает отложенных людей уровня не выше level к оставшейся нехватке:
        1 — своя команда, 2 — все подходящие."""
        ready = [item for item in self.pending if item[0] <= level]
        self.pending = [item for item in self.pending if item[0] > level]
        for _, eid, node, hours in ready:
            self._edge(("person", eid), node, hours)
        self._augment()

    def _augment(self):
        graph = self.graph
        while True:
            parents = {"source": None}
            queue = deque(["source"])
            while queue and "sink" not in parents:
                a = queue.popleft()
                neighbours = graph.get(a, {}).items()
                if a == "source":
                    # Самый свободный человек — первым.
                    neighbours = sorted(neighbours, key=lambda item: (-item[1], item[0][1]))
                for b, capacity in neighbours:
                    if capacity > EPS and b not in parents:
                        parents[b] = a
                        queue.append(b)
            if "sink" not in parents:
                return
            amount, node = float("inf"), "sink"
            while parents[node] is not None:
                amount = min(amount, graph[parents[node]][node])
                node = parents[node]
            node = "sink"
            while parents[node] is not None:
                prev = parents[node]
                graph[prev][node] -= amount
                graph[node][prev] += amount
                node = prev

    def placed(self, demand):
        node = ("role", demand)
        return self.initial[node, "sink"] - self.graph[node]["sink"]

    def rows(self):
        result = []
        for (a, b), cap in self.initial.items():
            if isinstance(a, tuple) and a[0] == "person" and isinstance(b, tuple) and b[0] == "role":
                used = cap - self.graph[a][b]
                if used > 1e-7:
                    result.append(dict(engineer=a[1], role=b[1], hours=used))
        return result


def allocate(demands, capacities, eligible):
    """Разовая раскладка: потребности роли → люди. Возвращает (полностью ли, строки)."""
    network = Network(capacities)
    for role, hours in sorted(demands.items()):
        network.add(role, hours, eligible.get(role, []))
    placed = sum(network.placed(role) for role in demands)
    return placed >= sum(demands.values()) - 1e-7, network.rows()
