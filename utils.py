from datetime import timedelta
ROUNDING_MINUTES = 5

def round_time(dt):
    total = dt.minute*60 + dt.second
    interval = ROUNDING_MINUTES*60
    rounded = int(round(total/interval)*interval)
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=rounded)


def compute_shifts(times):
    total = timedelta()
    for i in range(0, len(times)-1, 2):
        t_in, t_out = round_time(times[i]), round_time(times[i+1])
        dur = t_out - t_in
        if dur >= timedelta(hours=5): dur -= timedelta(minutes=30)
        total += dur
    reg = min(total, timedelta(hours=8))
    ot  = max(total - timedelta(hours=8), timedelta())
    return {'total': total, 'regular': reg, 'overtime': ot}