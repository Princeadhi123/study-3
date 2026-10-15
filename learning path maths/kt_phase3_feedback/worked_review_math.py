import ast
import math
import re
from fractions import Fraction


SOLUTION_VERSION = "assessment_worked_solution_v1"


def normalize(text):
    return " ".join(text.replace("\\n", " ").replace("\u2212", "-")
                    .replace("\u2044", "/").split())


def number(text):
    text = re.sub(r"\s*(?:€/l|€/kg|€|%)$", "", normalize(text))
    return Fraction(text.replace(",", ".").strip())


def display(value):
    value = Fraction(value)
    return str(value.numerator) if value.denominator == 1 else str(value)


def decimal_display(value):
    value = Fraction(value)
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return display(value)
    places = max(twos, fives)
    scaled = abs(value.numerator) * (10 ** places // value.denominator)
    digits = str(scaled).zfill(places + 1)
    result = (digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".") if places else digits
    return ("−" if value < 0 else "") + result


def arithmetic(expression, decimal=False):
    expression = expression.replace("⋅", "*").replace("·", "*")
    expression = expression.replace("×", "*").replace("÷", "/").replace(":", "/")
    if len(expression) > 120 or not re.fullmatch(r"[0-9.+*/()\s-]+", expression):
        raise ValueError("unsupported arithmetic expression")
    tree = ast.parse(expression.strip(), mode="eval")
    steps = []

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Fraction(ast.get_source_segment(expression.strip(), node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if not isinstance(node, ast.BinOp):
            raise ValueError("unsupported arithmetic operation")
        right = visit(node.right)
        left = visit(node.left)
        if isinstance(node.op, ast.Add):
            value, symbol = left + right, "+"
        elif isinstance(node.op, ast.Sub):
            value, symbol = left - right, "−"
        elif isinstance(node.op, ast.Mult):
            value, symbol = left * right, "×"
        elif isinstance(node.op, ast.Div):
            value, symbol = left / right, "÷"
        else:
            raise ValueError("unsupported arithmetic operation")
        formatter = decimal_display if decimal else display
        steps.append(f"{formatter(left)} {symbol} ({formatter(right)}) = {formatter(value)}.")
        return value

    return visit(tree.body), steps


WORD_RECIPES = {
    "10 litraa mansikoita maksaa 25 euroa. Kuinka paljon yksi litra mansikoita maksaa?":
        ("price_unit_price", "25 / 10", "Divide the total price by the number of litres to find euros per litre."),
    "250 gramman suklaapatukka maksaa 2,20 euroa. Mikä on suklaapatukan kilohinta?":
        ("price_unit_price", "2.20 / (250 / 1000)", "Convert the mass to kilograms, then divide the price by that mass to find euros per kilogram."),
    "Maria ostaa kolme litraa jäätelöä. Kymmenen litraa jäätelöä maksaa 25 euroa. Kuinka monta euroa Marian ostama jäätelö maksaa?":
        ("price_proportional_cost", "(25 / 10) * 3", "Find the price per litre, then multiply by the litres bought."),
    "8 litraa vaniljajäätelöä maksaa 32 euroa. Kuinka paljon maksaa 9 litraa vaniljajäätelöä?":
        ("price_proportional_cost", "(32 / 8) * 9", "Find the price per litre, then multiply by the litres bought."),
    "500 gramman karkkipussi maksaa 6,50 euroa. Kuinka paljon maksaa kilo karkkia?":
        ("price_unit_price", "6.50 / (500 / 1000)", "Convert the mass to kilograms, then divide the price by that mass to find euros per kilogram."),
    "Elsa ostaa 12 kappaletta haarukoita. 20 haarukkaa maksaa 40 euroa. Paljonko Elsan ostokset maksavat?":
        ("price_proportional_cost", "(40 / 20) * 12", "Find the price of one fork, then multiply by the number bought."),
    "Viisi kahvipakettia maksaa 20,25 euroa. Kuinka paljon maksaa yksi kahvipaketti?":
        ("price_unit_price", "20.25 / 5", "Divide the total price by the number of packets to find the price of one packet."),
    "2,5 desilitran limutölkki maksaa 2,65 euroa. Mikä on limun litrahinta?":
        ("price_unit_price", "2.65 / (2.5 / 10)", "Convert decilitres to litres, then divide the price by the volume to find euros per litre."),
    "2 litraa limua maksaa 3 euroa. Puolen litran pullo samaa limua maksaa 1,50 euroa. Kuinka paljon kalliimpi litrahinta on puolen litran pullossa kuin kahden litra pullossa?":
        ("price_unit_price", "(1.50 / 0.5) - (3 / 2)", "Find euros per litre for each bottle, then subtract the larger bottle's unit price from the smaller bottle's unit price."),
}
QUANTITY_STEM = (
    "Norjalaiset lohikäärmeet harrastavat silmukoiden tekemistä ilmassa. "
    "Nuoremmat lohikäärmeet osaavat tehdä peräkkäisiä silmukoita luonnollisesti "
    "vanhempia enemmän. Suurin peräkkäisten silmukoiden määrä on 28. "
    "Vanhimmat lohikäärmeet tekevät silmukoita parhaimmillaan vain neljäsosan "
    "parhaasta tuloksesta. Kuinka monta silmukkaa vanhat lohikäärmeet tekevät "
    "peräkkäin parhaimmillaan?")


def _algebra_terms(expression):
    compact = normalize(expression).replace(" ", "")
    if not compact:
        raise ValueError("unsupported algebra expression")
    terms = []
    start = 0
    while start < len(compact):
        position = start + int(compact[start] in "+-")
        digit_start = position
        while position < len(compact) and compact[position].isdecimal():
            position += 1
        has_digits = position > digit_start
        if position < len(compact) and "a" <= compact[position] <= "z":
            position += 1
        elif not has_digits:
            raise ValueError("unsupported algebra expression")
        if position < len(compact) and compact[position] not in "+-":
            raise ValueError("unsupported algebra expression")
        terms.append(compact[start:position])
        start = position
    return terms


def algebra(expression):
    terms = _algebra_terms(expression)
    coefficients = {}
    for term in terms:
        variable = term[-1] if term[-1].isalpha() else ""
        coefficient = term[:-1] if variable else term
        coefficient = {"": "1", "+": "1", "-": "-1"}.get(coefficient, coefficient)
        coefficients[variable] = coefficients.get(variable, 0) + int(coefficient)
    return {variable: coefficient for variable, coefficient in coefficients.items() if coefficient}


def _variable_term(variable, coefficient):
    if coefficient == 1:
        return variable
    if coefficient == -1:
        return "−" + variable
    return f"{coefficient}{variable}"


def _like_terms_solution(expression, expected):
    if len(expected) != 1 or "" in expected:
        raise ValueError("not a supported like-terms question")
    variable, coefficient = next(iter(expected.items()))
    parts = _algebra_terms(expression)
    if len(parts) != 2 or any(part[-1] != variable for part in parts):
        raise ValueError("not a supported two-term expression")
    values = [algebra(part).get(variable, 0) for part in parts]
    result = _variable_term(variable, coefficient)
    steps = [f"Both terms have variable part {variable}; keep that variable part.",
             f"Combine the signed coefficients: {values[0]} + ({values[1]}) = {coefficient}.",
             f"The simplified expression is {result}."]
    reasoning = "How did you identify matching variable parts, and why does the variable part stay unchanged?"
    return steps, reasoning


def _unlike_terms_solution(expression, expected):
    terms = _algebra_terms(expression)
    if len(expected) != 2 or len(terms) != 2:
        raise ValueError("not a supported two-term unlike-terms question")
    steps = ["The terms have different variable parts; a constant has no variable part.",
             "Only terms with matching variable parts can be combined.",
             f"The expression therefore remains {expression}."]
    reasoning = "What makes these terms unlike, and why would combining them change the expression?"
    return steps, reasoning


def _algebra_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"Sievenn[äa](?: lauseke:)?\s+(.+)", stem)
    if match is None:
        raise ValueError("unsupported algebra prompt")
    expression = match[1]
    expected = algebra(expression)
    if algebra(correct) != expected:
        raise ValueError("algebra solution disagrees with the bank")
    if subtopic_id == "combine_like_terms":
        steps, reasoning = _like_terms_solution(expression, expected)
    else:
        steps, reasoning = _unlike_terms_solution(expression, expected)
    return None, steps, reasoning


def _percentage_amount_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"(?:Kuinka paljon on )?(\d+) % luvusta (\d+)\??", stem)
    if match is None:
        raise ValueError("unsupported percentage prompt")
    percent, whole = map(int, match.groups())
    expected = Fraction(percent * whole, 100)
    steps = [f"The whole amount is {whole}.",
             f"Write {percent}% as {percent}/100 = {display(Fraction(percent, 100))}.",
             f"Multiply the whole by that fraction: {whole} × ({percent}/100) = {display(expected)}."]
    reasoning = "Which number is the whole, and why do we divide the percentage by one hundred before multiplying?"
    return expected, steps, reasoning


def _divisibility_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"(\d+) on jaollinen luvulla\.\.\.", stem)
    if match is None:
        raise ValueError("unsupported divisibility prompt")
    whole, divisor = int(match[1]), number(correct)
    if divisor.denominator != 1 or divisor <= 0 or whole % divisor:
        raise ValueError("divisor disagrees with the bank")
    quotient = Fraction(whole, divisor)
    expected = divisor
    steps = ["A divisor divides the whole number with no remainder.",
             f"{whole} ÷ {display(divisor)} = {display(quotient)}, with no remainder.",
             f"Check by multiplication: {display(divisor)} × {display(quotient)} = {whole}."]
    reasoning = "How does multiplication check that the offered number is a divisor?"
    return expected, steps, reasoning


def _prime_solution(question, stem, correct, subtopic_id):
    if stem != "Alkuluku?":
        raise ValueError("unsupported prime prompt")
    expected = number(correct)
    if expected.denominator != 1:
        raise ValueError("prime must be an integer")
    prime = int(expected)
    if prime < 2 or any(prime % divisor == 0 for divisor in range(2, math.isqrt(prime) + 1)):
        raise ValueError("prime disagrees with the bank")
    trials = list(range(2, math.isqrt(prime) + 1))
    steps = ["A prime is a whole number greater than one with exactly two positive divisors.",
             (f"For {prime}, test divisors {', '.join(map(str, trials))}; none divides it exactly."
              if trials else f"{prime} has no possible trial divisors between 2 and its square root."),
             f"The only positive divisors of {prime} are 1 and {prime}, so it is prime."]
    reasoning = "Why is having exactly two positive divisors the important condition?"
    return expected, steps, reasoning


def _fraction_quantity_solution(question, stem, correct, subtopic_id):
    if stem != QUANTITY_STEM:
        raise ValueError("unsupported fractional quantity prompt")
    expected = Fraction(28, 4)
    steps = ["The question asks for one quarter of 28 loops.",
             "Divide the whole into four equal parts: 28 ÷ 4 = 7.",
             "One quarter of the whole is therefore 7 loops."]
    reasoning = "Why does finding one quarter mean dividing the whole into four equal parts?"
    return expected, steps, reasoning


def _fraction_product_solution(a, b, c, d):
    expected = Fraction(a, b) * Fraction(c, d)
    steps = [f"Write the factors as {a}/{b} and {c}/{d}.",
             f"Multiply numerators and denominators: ({a} × {c})/({b} × {d}) = {a*c}/{b*d}.",
             f"An equivalent simplified result is {display(expected)}."]
    reasoning = "Which numbers are multiplied in the numerator and denominator, and how can you simplify the result?"
    return expected, steps, reasoning


def _fraction_division_solution(a, b, c, d):
    expected = Fraction(a, b) / Fraction(c, d)
    steps = [f"Dividing by {c} is multiplying by its reciprocal, 1/{c}.",
             f"({a}/{b}) × (1/{c}) = {a}/{b*c} = {display(expected)}."]
    reasoning = "Why do we multiply by the reciprocal when dividing by a whole number?"
    return expected, steps, reasoning


def _fraction_sum_solution(a, b, c, d):
    denominator = math.lcm(b, d)
    n1, n2 = a * (denominator // b), c * (denominator // d)
    expected = Fraction(a, b) + Fraction(c, d)
    steps = [f"Use a common denominator: {a}/{b} = {n1}/{denominator}, and {c}/{d} = {n2}/{denominator}.",
             f"Add the numerators and keep the denominator: ({n1} + {n2})/{denominator} = {n1+n2}/{denominator}.",
             f"An equivalent simplified result is {display(expected)}."]
    reasoning = "Why must the fractions refer to equal-sized parts before their numerators can be added?"
    return expected, steps, reasoning


def _fraction_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"(\d+)/(\d+)\s*([·*+:])\s*(\d+)(?:/(\d+))?", stem)
    if match is None:
        raise ValueError("unsupported fraction prompt")
    a, b, operator, c, d = match.groups()
    a, b, c, d = int(a), int(b), int(c), int(d or 1)
    if operator in ("·", "*") and subtopic_id in ("fraction_product", "fractions_multiply", "fraction_times_integer", "fractions_multiply_whole"):
        return _fraction_product_solution(a, b, c, d)
    if operator == ":" and d == 1 and subtopic_id == "fractions_divide_whole":
        return _fraction_division_solution(a, b, c, d)
    if operator == "+" and subtopic_id in ("fractions_add_same_denominator", "fractions_add_different_denominators"):
        return _fraction_sum_solution(a, b, c, d)
    raise ValueError("fraction operation does not match the subtopic")


def _fraction_compare_solution(question, stem, correct, subtopic_id):
    if stem != "Suurin murtoluku?":
        raise ValueError("no checked worked solution for this question")
    values = [number(option) for option in question["options"]]
    expected = max(values)
    denominator = math.lcm(*(value.denominator for value in values))
    numerators = [value.numerator * (denominator // value.denominator) for value in values]
    steps = [f"Use a common denominator of {denominator}.",
             "The offered fractions become " + ", ".join(f"{n}/{denominator}" for n in numerators) + ".",
             f"The largest numerator is {max(numerators)}, so the largest fraction is {display(expected)}."]
    reasoning = "Why can the numerators be compared directly once the denominators match?"
    return expected, steps, reasoning


def _arithmetic_solution(question, stem, correct, subtopic_id):
    expected, calculations = arithmetic(stem)
    steps = ["Evaluate brackets first, then multiplication and division, then addition and subtraction."] + calculations
    reasoning = "Explain which operation must be done first and how you can check the final result."
    return expected, steps, reasoning


def _price_subtraction_solution(question, stem, correct, subtopic_id):
    if stem != "10 € - 2,5 €":
        raise ValueError("no checked worked solution for this question")
    expected, calculations = arithmetic("10 - 2.5", decimal=True)
    steps = ["Align the decimal places and subtract the smaller price from the larger price."] + calculations
    reasoning = "How does aligning decimal places help you subtract these prices accurately?"
    return expected, steps, reasoning


def _word_price_solution(question, stem, correct, subtopic_id):
    if stem not in WORD_RECIPES or WORD_RECIPES[stem][0] != subtopic_id:
        raise ValueError("no checked worked solution for this question")
    _, expression, instruction = WORD_RECIPES[stem]
    expected, calculations = arithmetic(expression, decimal=True)
    steps = [instruction] + calculations
    reasoning = "What are the units at each step, and why is the division or multiplication appropriate?"
    return expected, steps, reasoning


def _percent_whole_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"Mistä (?:rahamäärästä|luvusta) (\d+) % on (?:luku )?(\d+)(?: €)?\?", stem)
    increase = re.fullmatch(r"Tuotteen hintaa korotettiin (\d+) % eli (\d+) €. Mikä oli alkuperäinen hinta\?", stem)
    match = match or increase
    if match is None:
        raise ValueError("unsupported percentage-whole prompt")
    percent, part = map(int, match.groups())
    expected = Fraction(part * 100, percent)
    steps = [f"The part {part} represents {percent}% of the original whole.",
             f"Write the rate as {percent}/100.",
             f"Divide the part by the rate: {part} ÷ ({percent}/100) = {display(expected)}."]
    reasoning = "Why do we divide the given part by the percentage multiplier to recover the whole?"
    return expected, steps, reasoning


def _percent_multiplier_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"Mikä on prosenttiluvun (\d+) % prosenttikerroin\?", stem)
    if match is None:
        raise ValueError("unsupported percentage-multiplier prompt")
    percent = int(match[1])
    expected = Fraction(percent, 100)
    steps = ["Percent means out of one hundred.", f"Divide by 100: {percent}/100 = {decimal_display(expected)}."]
    reasoning = "How does this multiplier represent the same proportion as the percentage?"
    return expected, steps, reasoning


def _percent_rate_solution(question, stem, correct, subtopic_id):
    match = re.fullmatch(r"Montako prosenttia luku (\d+) on luvusta (\d+)\?", stem)
    if match is None:
        raise ValueError("unsupported percentage-rate prompt")
    part, whole = map(int, match.groups())
    expected = Fraction(part * 100, whole)
    steps = [f"Identify the part {part} and the whole {whole}.",
             f"Divide the part by the whole: {part}/{whole} = {display(Fraction(part, whole))}.",
             f"Multiply by 100 to express the ratio as a percentage: ({part}/{whole}) × 100 = {display(expected)}%."]
    reasoning = "Which number is the whole, and why is the part-to-whole ratio multiplied by one hundred?"
    return expected, steps, reasoning


_SOLUTION_HANDLERS = {
    "combine_like_terms": _algebra_solution,
    "retain_unlike_terms": _algebra_solution,
    "percentage_amount": _percentage_amount_solution,
    "percent_of_number": _percentage_amount_solution,
    "divisibility": _divisibility_solution,
    "prime_recognition": _prime_solution,
    "fraction_of_quantity": _fraction_quantity_solution,
    "fraction_product": _fraction_solution,
    "fractions_multiply": _fraction_solution,
    "fraction_times_integer": _fraction_solution,
    "fractions_multiply_whole": _fraction_solution,
    "fractions_divide_whole": _fraction_solution,
    "fractions_add_same_denominator": _fraction_solution,
    "fractions_add_different_denominators": _fraction_solution,
    "fractions_compare": _fraction_compare_solution,
    "arithmetic_order_of_operations": _arithmetic_solution,
    "arithmetic_division_by_ten": _arithmetic_solution,
    "price_subtraction": _price_subtraction_solution,
    "price_unit_price": _word_price_solution,
    "price_proportional_cost": _word_price_solution,
    "percent_find_whole": _percent_whole_solution,
    "percent_as_multiplier": _percent_multiplier_solution,
    "percent_find_rate": _percent_rate_solution}


GENERIC_SOCRATIC_PROMPT = (
    "How did you approach this question? Explain the steps you tried to your teacher.")

PRE_REVEAL_SCAFFOLDS = {
    "percentage_amount": (
        "What is the whole amount, and what does the stated percentage mean? How would you use those ideas to find the requested amount?",
        "Identify the whole amount and the stated percentage.",
        "Percent means out of one hundred. How could you write the percentage as a fraction and apply it to the whole?"),
    "combine_like_terms": (
        "What do the terms have in common, and how would you use that to simplify the expression?",
        "Look separately at the number multiplying each variable and at the variable part.",
        "Only matching variable parts can be collected. How would you combine their signed coefficients while keeping the variable part?"),
    "retain_unlike_terms": (
        "How would you decide whether these terms can be combined?",
        "Compare the variable parts of the terms; a constant has no variable part.",
        "Compare both the letters and any powers on the letters. What would have to be true for the terms to represent the same kind of quantity?"),
    "divisibility": (
        "How would you check whether an offered number divides the given whole number exactly?",
        "Consider each offered number as a possible divisor.",
        "Use division or multiplication to check for a whole-number multiple with no remainder."),
    "prime_recognition": (
        "What makes a number prime, and how would you check the offered numbers?",
        "Think about the positive divisors of each offered number.",
        "A prime number is greater than one and has exactly two positive divisors. Which divisor checks would help you decide?"),
    "fraction_of_quantity": (
        "What is the whole quantity, and how does the requested fraction describe a part of it?",
        "Identify the whole quantity and the fraction requested by the wording.",
        "Connect the fraction to equal parts of the whole. Which calculation would give the requested number of those parts?"),
    "fraction_product": (
        "How would you use the numerators and denominators to work out this product?",
        "Identify the numerator and denominator of each fraction.",
        "Consider what happens to the numerator and denominator when fractions are multiplied, then check whether the result can be simplified."),
    "fraction_times_integer": (
        "How could you represent the whole number so that you can multiply it by the fraction?",
        "Identify the fraction and the whole-number multiplier.",
        "Represent the whole number as a fraction with denominator one. How does that help you use fraction multiplication?"),
    "fractions_divide_whole": (
        "What does dividing this fraction by the whole number mean, and how could you represent the calculation?",
        "Identify the fraction being divided and the whole-number divisor.",
        "Think about multiplication by the reciprocal of the divisor. How could you check your result by multiplication?"),
    "fractions_add_same_denominator": (
        "What does the shared denominator tell you about the parts being added?",
        "Compare the denominators and identify the size of the parts.",
        "When the part sizes match, think about how to combine the number of parts without changing their size."),
    "fractions_add_different_denominators": (
        "How could you express these fractions using parts of the same size before adding them?",
        "Compare the denominators and look for a common denominator.",
        "Rewrite each fraction as an equivalent fraction with that denominator, then think about combining the numerators."),
    "fractions_compare": (
        "How could you compare the sizes of these fractions rather than just their numerators?",
        "Think about a number line or a common denominator.",
        "Express the fractions with equal-sized parts, then compare how many of those parts each fraction contains."),
    "arithmetic_order_of_operations": (
        "Which operation would you carry out first, and what rule supports that choice?",
        "Look for brackets and identify the operations in the expression.",
        "Consider brackets first, then multiplication and division from left to right, then addition and subtraction from left to right."),
    "arithmetic_division_by_ten": (
        "How would dividing by ten affect the place value of each digit?",
        "Identify each digit's place value before calculating.",
        "Use a place-value table to track the change, then consider how multiplying back by ten could check your result."),
    "price_subtraction": (
        "How would you arrange these prices before finding their difference?",
        "Check that the prices use the same currency unit.",
        "Align the decimal places and subtract corresponding place values. How could addition check the difference?"),
    "price_unit_price": (
        "What quantity does the total price cover, and how would you find the price of a single unit?",
        "Identify the total price and the quantity it pays for.",
        "Think about dividing the total price by the quantity. What unit should your result have?"),
    "price_proportional_cost": (
        "How could you use the price of a single unit to find the cost of the requested quantity?",
        "Identify the quantity and cost that are already given.",
        "Find the price of a single unit, then consider how to scale it to the requested quantity."),
    "percent_find_whole": (
        "Which quantity is the part, and which whole amount are you trying to find?",
        "Identify the known part and the percentage it represents.",
        "Express the percentage as a decimal and think about reversing the multiplication that would produce the part."),
    "percent_as_multiplier": (
        "How could you represent the percentage as a multiplier?",
        "Recall what percent means in relation to one hundred.",
        "Consider dividing the percentage by one hundred to express the same proportion as a decimal."),
    "percent_find_rate": (
        "Which numbers are the part and the whole, and how would you express their relationship as a percentage?",
        "Identify the part and the whole before choosing an operation.",
        "Form the part-to-whole ratio, then consider how to express that ratio out of one hundred."),
}
PRE_REVEAL_ALIASES = {
    "percent_of_number": "percentage_amount",
    "fractions_multiply": "fraction_product",
    "fractions_multiply_whole": "fraction_times_integer",
}


def pre_reveal_scaffold(subtopic_id):
    key = PRE_REVEAL_ALIASES.get(subtopic_id, subtopic_id)
    wording = PRE_REVEAL_SCAFFOLDS.get(key)
    if wording is None:
        return {"socratic_prompt": GENERIC_SOCRATIC_PROMPT, "progressive_hints": []}
    return {"socratic_prompt": wording[0], "progressive_hints": list(wording[1:])}


def solution(question, subtopic_id):
    stem = normalize(question["text"])
    correct = question["options"][question["answer_index"]]
    handler = _SOLUTION_HANDLERS.get(subtopic_id)
    if handler is None:
        raise ValueError("no checked worked solution for this question")
    expected, steps, reasoning = handler(question, stem, correct, subtopic_id)
    if expected is not None and number(correct) != expected:
        raise ValueError("worked solution disagrees with the bank answer")
    return {"steps": steps, "reasoning_question": reasoning,
            "solution_version": SOLUTION_VERSION}
